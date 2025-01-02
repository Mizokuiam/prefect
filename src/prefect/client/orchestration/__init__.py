import asyncio
import base64
import datetime
import ssl
import warnings
from collections.abc import Iterable
from contextlib import AsyncExitStack
from logging import Logger
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Optional, Union, overload
from uuid import UUID, uuid4

import certifi
import httpcore
import httpx
import pendulum
import pydantic
from asgi_lifespan import LifespanManager
from packaging import version
from starlette import status
from typing_extensions import ParamSpec, Self, TypeVar

from prefect.client.orchestration._artifacts.client import (
    ArtifactClient,
    ArtifactAsyncClient,
    ArtifactCollectionClient,
    ArtifactCollectionAsyncClient,
)

from prefect.client.orchestration._concurrency_limits.client import (
    ConcurrencyLimitAsyncClient,
    ConcurrencyLimitClient,
)

from prefect.client.orchestration._logs.client import (
    LogClient,
    LogAsyncClient,
)
from prefect.client.orchestration._variables.client import (
    VariableClient,
    VariableAsyncClient,
)

import prefect
import prefect.exceptions
import prefect.settings
import prefect.states
from prefect._experimental.sla import SlaTypes
from prefect.client.constants import SERVER_API_VERSION
from prefect.client.schemas import FlowRun, OrchestrationResult, TaskRun, sorting
from prefect.client.schemas.actions import (
    BlockDocumentCreate,
    BlockDocumentUpdate,
    BlockSchemaCreate,
    BlockTypeCreate,
    BlockTypeUpdate,
    DeploymentCreate,
    DeploymentFlowRunCreate,
    DeploymentScheduleCreate,
    DeploymentScheduleUpdate,
    DeploymentUpdate,
    FlowCreate,
    FlowRunCreate,
    FlowRunNotificationPolicyCreate,
    FlowRunNotificationPolicyUpdate,
    FlowRunUpdate,
    LogCreate,
    GlobalConcurrencyLimitCreate,
    GlobalConcurrencyLimitUpdate,
    TaskRunCreate,
    TaskRunUpdate,
    WorkPoolCreate,
    WorkPoolUpdate,
    WorkQueueCreate,
    WorkQueueUpdate,
)
from prefect.client.schemas.filters import (
    DeploymentFilter,
    FlowFilter,
    FlowRunFilter,
    FlowRunNotificationPolicyFilter,
    TaskRunFilter,
    WorkerFilter,
    WorkPoolFilter,
    WorkQueueFilter,
    WorkQueueFilterName,
)
from prefect.client.schemas.objects import (
    BlockDocument,
    BlockSchema,
    BlockType,
    ConcurrencyOptions,
    Constant,
    DeploymentSchedule,
    Flow,
    FlowRunInput,
    FlowRunNotificationPolicy,
    FlowRunPolicy,
    Parameter,
    TaskRunPolicy,
    TaskRunResult,
    Worker,
    WorkerMetadata,
    WorkPool,
    WorkQueue,
    WorkQueueStatusDetail,
)
from prefect.client.schemas.responses import (
    DeploymentResponse,
    FlowRunResponse,
    WorkerFlowRunResponse,
)
from prefect.client.schemas.schedules import SCHEDULE_TYPES
from prefect.client.schemas.sorting import (
    DeploymentSort,
    FlowRunSort,
    FlowSort,
    TaskRunSort,
)
from prefect.events import filters
from prefect.events.schemas.automations import Automation, AutomationCore
from prefect.logging import get_logger
from prefect.settings import (
    PREFECT_API_AUTH_STRING,
    PREFECT_API_DATABASE_CONNECTION_URL,
    PREFECT_API_ENABLE_HTTP2,
    PREFECT_API_KEY,
    PREFECT_API_REQUEST_TIMEOUT,
    PREFECT_API_SSL_CERT_FILE,
    PREFECT_API_TLS_INSECURE_SKIP_VERIFY,
    PREFECT_API_URL,
    PREFECT_CLIENT_CSRF_SUPPORT_ENABLED,
    PREFECT_CLOUD_API_URL,
    PREFECT_SERVER_ALLOW_EPHEMERAL_MODE,
    PREFECT_TESTING_UNIT_TEST_MODE,
    get_current_settings,
)
from prefect.types import KeyValueLabelsField

if TYPE_CHECKING:
    from prefect.flows import Flow as FlowObject
    from prefect.tasks import Task as TaskObject

from prefect.client.base import (
    ASGIApp,
    PrefectHttpxAsyncClient,
    PrefectHttpxSyncClient,
    ServerType,
    app_lifespan_context,
)

P = ParamSpec("P")
R = TypeVar("R", infer_variance=True)
T = TypeVar("T")


@overload
def get_client(
    *,
    httpx_settings: Optional[dict[str, Any]] = ...,
    sync_client: Literal[False] = False,
) -> "PrefectClient":
    ...


@overload
def get_client(
    *, httpx_settings: Optional[dict[str, Any]] = ..., sync_client: Literal[True] = ...
) -> "SyncPrefectClient":
    ...


def get_client(
    httpx_settings: Optional[dict[str, Any]] = None, sync_client: bool = False
) -> Union["SyncPrefectClient", "PrefectClient"]:
    """
    Retrieve a HTTP client for communicating with the Prefect REST API.

    The client must be context managed; for example:

    ```python
    async with get_client() as client:
        await client.hello()
    ```

    To return a synchronous client, pass sync_client=True:

    ```python
    with get_client(sync_client=True) as client:
        client.hello()
    ```
    """
    import prefect.context

    # try to load clients from a client context, if possible
    # only load clients that match the provided config / loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if sync_client:
        if client_ctx := prefect.context.SyncClientContext.get():
            if (
                client_ctx.client
                and getattr(client_ctx, "_httpx_settings", None) == httpx_settings
            ):
                return client_ctx.client
    else:
        if client_ctx := prefect.context.AsyncClientContext.get():
            if (
                client_ctx.client
                and getattr(client_ctx, "_httpx_settings", None) == httpx_settings
                and loop in (getattr(client_ctx.client, "_loop", None), None)
            ):
                return client_ctx.client

    api: str = PREFECT_API_URL.value()
    server_type = None

    if not api and PREFECT_SERVER_ALLOW_EPHEMERAL_MODE:
        # create an ephemeral API if none was provided
        from prefect.server.api.server import SubprocessASGIServer

        server = SubprocessASGIServer()
        server.start()
        assert server.server_process is not None, "Server process did not start"

        api = server.api_url
        server_type = ServerType.EPHEMERAL
    elif not api and not PREFECT_SERVER_ALLOW_EPHEMERAL_MODE:
        raise ValueError(
            "No Prefect API URL provided. Please set PREFECT_API_URL to the address of a running Prefect server."
        )

    if sync_client:
        return SyncPrefectClient(
            api,
            auth_string=PREFECT_API_AUTH_STRING.value(),
            api_key=PREFECT_API_KEY.value(),
            httpx_settings=httpx_settings,
            server_type=server_type,
        )
    else:
        return PrefectClient(
            api,
            auth_string=PREFECT_API_AUTH_STRING.value(),
            api_key=PREFECT_API_KEY.value(),
            httpx_settings=httpx_settings,
            server_type=server_type,
        )


class PrefectClient(
    ArtifactAsyncClient,
    ArtifactCollectionAsyncClient,
    LogAsyncClient,
    VariableAsyncClient,
    ConcurrencyLimitAsyncClient,
):
    """
    An asynchronous client for interacting with the [Prefect REST API](/api-ref/rest-api/).

    Args:
        api: the REST API URL or FastAPI application to connect to
        api_key: An optional API key for authentication.
        api_version: The API version this client is compatible with.
        httpx_settings: An optional dictionary of settings to pass to the underlying
            `httpx.AsyncClient`

    Examples:

        Say hello to a Prefect REST API

        <div class="terminal">
        ```
        >>> async with get_client() as client:
        >>>     response = await client.hello()
        >>>
        >>> print(response.json())
        👋
        ```
        </div>
    """

    def __init__(
        self,
        api: Union[str, ASGIApp],
        *,
        auth_string: Optional[str] = None,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
        httpx_settings: Optional[dict[str, Any]] = None,
        server_type: Optional[ServerType] = None,
    ) -> None:
        httpx_settings = httpx_settings.copy() if httpx_settings else {}
        httpx_settings.setdefault("headers", {})

        if PREFECT_API_TLS_INSECURE_SKIP_VERIFY:
            # Create an unverified context for insecure connections
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            httpx_settings.setdefault("verify", ctx)
        else:
            cert_file = PREFECT_API_SSL_CERT_FILE.value()
            if not cert_file:
                cert_file = certifi.where()
            # Create a verified context with the certificate file
            ctx = ssl.create_default_context(cafile=cert_file)
            httpx_settings.setdefault("verify", ctx)

        if api_version is None:
            api_version = SERVER_API_VERSION
        httpx_settings["headers"].setdefault("X-PREFECT-API-VERSION", api_version)
        if api_key:
            httpx_settings["headers"].setdefault("Authorization", f"Bearer {api_key}")

        if auth_string:
            token = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
            httpx_settings["headers"].setdefault("Authorization", f"Basic {token}")

        # Context management
        self._context_stack: int = 0
        self._exit_stack = AsyncExitStack()
        self._ephemeral_app: Optional[ASGIApp] = None
        self.manage_lifespan = True
        self.server_type: ServerType

        # Only set if this client started the lifespan of the application
        self._ephemeral_lifespan: Optional[LifespanManager] = None

        self._closed = False
        self._started = False

        # Connect to an external application
        if isinstance(api, str):
            if httpx_settings.get("app"):
                raise ValueError(
                    "Invalid httpx settings: `app` cannot be set when providing an "
                    "api url. `app` is only for use with ephemeral instances. Provide "
                    "it as the `api` parameter instead."
                )
            httpx_settings.setdefault("base_url", api)

            # See https://www.python-httpx.org/advanced/#pool-limit-configuration
            httpx_settings.setdefault(
                "limits",
                httpx.Limits(
                    # We see instability when allowing the client to open many connections at once.
                    # Limiting concurrency results in more stable performance.
                    max_connections=16,
                    max_keepalive_connections=8,
                    # The Prefect Cloud LB will keep connections alive for 30s.
                    # Only allow the client to keep connections alive for 25s.
                    keepalive_expiry=25,
                ),
            )

            # See https://www.python-httpx.org/http2/
            # Enabling HTTP/2 support on the client does not necessarily mean that your requests
            # and responses will be transported over HTTP/2, since both the client and the server
            # need to support HTTP/2. If you connect to a server that only supports HTTP/1.1 the
            # client will use a standard HTTP/1.1 connection instead.
            httpx_settings.setdefault("http2", PREFECT_API_ENABLE_HTTP2.value())

            if server_type:
                self.server_type = server_type
            else:
                self.server_type = (
                    ServerType.CLOUD
                    if api.startswith(PREFECT_CLOUD_API_URL.value())
                    else ServerType.SERVER
                )

        # Connect to an in-process application
        else:
            self._ephemeral_app = api
            self.server_type = ServerType.EPHEMERAL

            # When using an ephemeral server, server-side exceptions can be raised
            # client-side breaking all of our response error code handling. To work
            # around this, we create an ASGI transport with application exceptions
            # disabled instead of using the application directly.
            # refs:
            # - https://github.com/PrefectHQ/prefect/pull/9637
            # - https://github.com/encode/starlette/blob/d3a11205ed35f8e5a58a711db0ff59c86fa7bb31/starlette/middleware/errors.py#L184
            # - https://github.com/tiangolo/fastapi/blob/8cc967a7605d3883bd04ceb5d25cc94ae079612f/fastapi/applications.py#L163-L164
            httpx_settings.setdefault(
                "transport",
                httpx.ASGITransport(
                    app=self._ephemeral_app, raise_app_exceptions=False
                ),
            )
            httpx_settings.setdefault("base_url", "http://ephemeral-prefect/api")

        # See https://www.python-httpx.org/advanced/#timeout-configuration
        httpx_settings.setdefault(
            "timeout",
            httpx.Timeout(
                connect=PREFECT_API_REQUEST_TIMEOUT.value(),
                read=PREFECT_API_REQUEST_TIMEOUT.value(),
                write=PREFECT_API_REQUEST_TIMEOUT.value(),
                pool=PREFECT_API_REQUEST_TIMEOUT.value(),
            ),
        )

        if not PREFECT_TESTING_UNIT_TEST_MODE:
            httpx_settings.setdefault("follow_redirects", True)

        enable_csrf_support = (
            self.server_type != ServerType.CLOUD
            and PREFECT_CLIENT_CSRF_SUPPORT_ENABLED.value()
        )

        self._client = PrefectHttpxAsyncClient(
            **httpx_settings, enable_csrf_support=enable_csrf_support
        )
        self._loop = None

        # See https://www.python-httpx.org/advanced/#custom-transports
        #
        # If we're using an HTTP/S client (not the ephemeral client), adjust the
        # transport to add retries _after_ it is instantiated. If we alter the transport
        # before instantiation, the transport will not be aware of proxies unless we
        # reproduce all of the logic to make it so.
        #
        # Only alter the transport to set our default of 3 retries, don't modify any
        # transport a user may have provided via httpx_settings.
        #
        # Making liberal use of getattr and isinstance checks here to avoid any
        # surprises if the internals of httpx or httpcore change on us
        if isinstance(api, str) and not httpx_settings.get("transport"):
            transport_for_url = getattr(self._client, "_transport_for_url", None)
            if callable(transport_for_url):
                server_transport = transport_for_url(httpx.URL(api))
                if isinstance(server_transport, httpx.AsyncHTTPTransport):
                    pool = getattr(server_transport, "_pool", None)
                    if isinstance(pool, httpcore.AsyncConnectionPool):
                        setattr(pool, "_retries", 3)

        self.logger: Logger = get_logger("client")

    @property
    def api_url(self) -> httpx.URL:
        """
        Get the base URL for the API.
        """
        return self._client.base_url

    # API methods ----------------------------------------------------------------------

    async def api_healthcheck(self) -> Optional[Exception]:
        """
        Attempts to connect to the API and returns the encountered exception if not
        successful.

        If successful, returns `None`.
        """
        try:
            await self._client.get("/health")
            return None
        except Exception as exc:
            return exc

    async def hello(self) -> httpx.Response:
        """
        Send a GET request to /hello for testing purposes.
        """
        return await self._client.get("/hello")

    async def create_flow(self, flow: "FlowObject[Any, Any]") -> UUID:
        """
        Create a flow in the Prefect API.

        Args:
            flow: a [Flow][prefect.flows.Flow] object

        Raises:
            httpx.RequestError: if a flow was not created for any reason

        Returns:
            the ID of the flow in the backend
        """
        return await self.create_flow_from_name(flow.name)

    async def create_flow_from_name(self, flow_name: str) -> UUID:
        """
        Create a flow in the Prefect API.

        Args:
            flow_name: the name of the new flow

        Raises:
            httpx.RequestError: if a flow was not created for any reason

        Returns:
            the ID of the flow in the backend
        """
        flow_data = FlowCreate(name=flow_name)
        response = await self._client.post(
            "/flows/", json=flow_data.model_dump(mode="json")
        )

        flow_id = response.json().get("id")
        if not flow_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        # Return the id of the created flow
        return UUID(flow_id)

    async def read_flow(self, flow_id: UUID) -> Flow:
        """
        Query the Prefect API for a flow by id.

        Args:
            flow_id: the flow ID of interest

        Returns:
            a [Flow model][prefect.client.schemas.objects.Flow] representation of the flow
        """
        response = await self._client.get(f"/flows/{flow_id}")
        return Flow.model_validate(response.json())

    async def delete_flow(self, flow_id: UUID) -> None:
        """
        Delete a flow by UUID.

        Args:
            flow_id: ID of the flow to be deleted
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fail
        """
        try:
            await self._client.delete(f"/flows/{flow_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_flows(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        work_pool_filter: Optional[WorkPoolFilter] = None,
        work_queue_filter: Optional[WorkQueueFilter] = None,
        sort: Optional[FlowSort] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Flow]:
        """
        Query the Prefect API for flows. Only flows matching all criteria will
        be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            work_pool_filter: filter criteria for work pools
            work_queue_filter: filter criteria for work pool queues
            sort: sort criteria for the flows
            limit: limit for the flow query
            offset: offset for the flow query

        Returns:
            a list of Flow model representations of the flows
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "work_pools": (
                work_pool_filter.model_dump(mode="json") if work_pool_filter else None
            ),
            "work_queues": (
                work_queue_filter.model_dump(mode="json") if work_queue_filter else None
            ),
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }

        response = await self._client.post("/flows/filter", json=body)
        return pydantic.TypeAdapter(list[Flow]).validate_python(response.json())

    async def read_flow_by_name(
        self,
        flow_name: str,
    ) -> Flow:
        """
        Query the Prefect API for a flow by name.

        Args:
            flow_name: the name of a flow

        Returns:
            a fully hydrated Flow model
        """
        response = await self._client.get(f"/flows/name/{flow_name}")
        return Flow.model_validate(response.json())

    async def create_flow_run_from_deployment(
        self,
        deployment_id: UUID,
        *,
        parameters: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        state: Optional[prefect.states.State[Any]] = None,
        name: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        idempotency_key: Optional[str] = None,
        parent_task_run_id: Optional[UUID] = None,
        work_queue_name: Optional[str] = None,
        job_variables: Optional[dict[str, Any]] = None,
        labels: Optional[KeyValueLabelsField] = None,
    ) -> FlowRun:
        """
        Create a flow run for a deployment.

        Args:
            deployment_id: The deployment ID to create the flow run from
            parameters: Parameter overrides for this flow run. Merged with the
                deployment defaults
            context: Optional run context data
            state: The initial state for the run. If not provided, defaults to
                `Scheduled` for now. Should always be a `Scheduled` type.
            name: An optional name for the flow run. If not provided, the server will
                generate a name.
            tags: An optional iterable of tags to apply to the flow run; these tags
                are merged with the deployment's tags.
            idempotency_key: Optional idempotency key for creation of the flow run.
                If the key matches the key of an existing flow run, the existing run will
                be returned instead of creating a new one.
            parent_task_run_id: if a subflow run is being created, the placeholder task
                run identifier in the parent flow
            work_queue_name: An optional work queue name to add this run to. If not provided,
                will default to the deployment's set work queue.  If one is provided that does not
                exist, a new work queue will be created within the deployment's work pool.
            job_variables: Optional variables that will be supplied to the flow run job.

        Raises:
            httpx.RequestError: if the Prefect API does not successfully create a run for any reason

        Returns:
            The flow run model
        """
        parameters = parameters or {}
        context = context or {}
        state = state or prefect.states.Scheduled()
        tags = tags or []

        flow_run_create = DeploymentFlowRunCreate(
            parameters=parameters,
            context=context,
            state=state.to_state_create(),
            tags=list(tags),
            name=name,
            idempotency_key=idempotency_key,
            parent_task_run_id=parent_task_run_id,
            job_variables=job_variables,
            labels=labels,
        )

        # done separately to avoid including this field in payloads sent to older API versions
        if work_queue_name:
            flow_run_create.work_queue_name = work_queue_name

        response = await self._client.post(
            f"/deployments/{deployment_id}/create_flow_run",
            json=flow_run_create.model_dump(mode="json", exclude_unset=True),
        )
        return FlowRun.model_validate(response.json())

    async def create_flow_run(
        self,
        flow: "FlowObject[Any, R]",
        name: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        tags: Optional[Iterable[str]] = None,
        parent_task_run_id: Optional[UUID] = None,
        state: Optional["prefect.states.State[R]"] = None,
    ) -> FlowRun:
        """
        Create a flow run for a flow.

        Args:
            flow: The flow model to create the flow run for
            name: An optional name for the flow run
            parameters: Parameter overrides for this flow run.
            context: Optional run context data
            tags: a list of tags to apply to this flow run
            parent_task_run_id: if a subflow run is being created, the placeholder task
                run identifier in the parent flow
            state: The initial state for the run. If not provided, defaults to
                `Scheduled` for now. Should always be a `Scheduled` type.

        Raises:
            httpx.RequestError: if the Prefect API does not successfully create a run for any reason

        Returns:
            The flow run model
        """
        parameters = parameters or {}
        context = context or {}

        if state is None:
            state = prefect.states.Pending()

        # Retrieve the flow id
        flow_id = await self.create_flow(flow)

        flow_run_create = FlowRunCreate(
            flow_id=flow_id,
            flow_version=flow.version,
            name=name,
            parameters=parameters,
            context=context,
            tags=list(tags or []),
            parent_task_run_id=parent_task_run_id,
            state=state.to_state_create(),
            empirical_policy=FlowRunPolicy(
                retries=flow.retries,
                retry_delay=int(flow.retry_delay_seconds or 0),
            ),
        )

        flow_run_create_json = flow_run_create.model_dump(mode="json")
        response = await self._client.post("/flow_runs/", json=flow_run_create_json)
        flow_run = FlowRun.model_validate(response.json())

        # Restore the parameters to the local objects to retain expectations about
        # Python objects
        flow_run.parameters = parameters

        return flow_run

    async def update_flow_run(
        self,
        flow_run_id: UUID,
        flow_version: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        name: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        empirical_policy: Optional[FlowRunPolicy] = None,
        infrastructure_pid: Optional[str] = None,
        job_variables: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        """
        Update a flow run's details.

        Args:
            flow_run_id: The identifier for the flow run to update.
            flow_version: A new version string for the flow run.
            parameters: A dictionary of parameter values for the flow run. This will not
                be merged with any existing parameters.
            name: A new name for the flow run.
            empirical_policy: A new flow run orchestration policy. This will not be
                merged with any existing policy.
            tags: An iterable of new tags for the flow run. These will not be merged with
                any existing tags.
            infrastructure_pid: The id of flow run as returned by an
                infrastructure block.

        Returns:
            an `httpx.Response` object from the PATCH request
        """
        params: dict[str, Any] = {}
        if flow_version is not None:
            params["flow_version"] = flow_version
        if parameters is not None:
            params["parameters"] = parameters
        if name is not None:
            params["name"] = name
        if tags is not None:
            params["tags"] = tags
        if empirical_policy is not None:
            params["empirical_policy"] = empirical_policy
        if infrastructure_pid:
            params["infrastructure_pid"] = infrastructure_pid
        if job_variables is not None:
            params["job_variables"] = job_variables

        flow_run_data = FlowRunUpdate(**params)

        return await self._client.patch(
            f"/flow_runs/{flow_run_id}",
            json=flow_run_data.model_dump(mode="json", exclude_unset=True),
        )

    async def delete_flow_run(
        self,
        flow_run_id: UUID,
    ) -> None:
        """
        Delete a flow run by UUID.

        Args:
            flow_run_id: The flow run UUID of interest.
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        try:
            await self._client.delete(f"/flow_runs/{flow_run_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def create_work_queue(
        self,
        name: str,
        description: Optional[str] = None,
        is_paused: Optional[bool] = None,
        concurrency_limit: Optional[int] = None,
        priority: Optional[int] = None,
        work_pool_name: Optional[str] = None,
    ) -> WorkQueue:
        """
        Create a work queue.

        Args:
            name: a unique name for the work queue
            description: An optional description for the work queue.
            is_paused: Whether or not the work queue is paused.
            concurrency_limit: An optional concurrency limit for the work queue.
            priority: The queue's priority. Lower values are higher priority (1 is the highest).
            work_pool_name: The name of the work pool to use for this queue.

        Raises:
            prefect.exceptions.ObjectAlreadyExists: If request returns 409
            httpx.RequestError: If request fails

        Returns:
            The created work queue
        """
        create_model = WorkQueueCreate(name=name, filter=None)
        if description is not None:
            create_model.description = description
        if is_paused is not None:
            create_model.is_paused = is_paused
        if concurrency_limit is not None:
            create_model.concurrency_limit = concurrency_limit
        if priority is not None:
            create_model.priority = priority

        data = create_model.model_dump(mode="json")
        try:
            if work_pool_name is not None:
                response = await self._client.post(
                    f"/work_pools/{work_pool_name}/queues", json=data
                )
            else:
                response = await self._client.post("/work_queues/", json=data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_409_CONFLICT:
                raise prefect.exceptions.ObjectAlreadyExists(http_exc=e) from e
            elif e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return WorkQueue.model_validate(response.json())

    async def read_work_queue_by_name(
        self,
        name: str,
        work_pool_name: Optional[str] = None,
    ) -> WorkQueue:
        """
        Read a work queue by name.

        Args:
            name (str): a unique name for the work queue
            work_pool_name (str, optional): the name of the work pool
                the queue belongs to.

        Raises:
            prefect.exceptions.ObjectNotFound: if no work queue is found
            httpx.HTTPStatusError: other status errors

        Returns:
            WorkQueue: a work queue API object
        """
        try:
            if work_pool_name is not None:
                response = await self._client.get(
                    f"/work_pools/{work_pool_name}/queues/{name}"
                )
            else:
                response = await self._client.get(f"/work_queues/name/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

        return WorkQueue.model_validate(response.json())

    async def update_work_queue(self, id: UUID, **kwargs: Any) -> None:
        """
        Update properties of a work queue.

        Args:
            id: the ID of the work queue to update
            **kwargs: the fields to update

        Raises:
            ValueError: if no kwargs are provided
            prefect.exceptions.ObjectNotFound: if request returns 404
            httpx.RequestError: if the request fails

        """
        if not kwargs:
            raise ValueError("No fields provided to update.")

        data = WorkQueueUpdate(**kwargs).model_dump(mode="json", exclude_unset=True)
        try:
            await self._client.patch(f"/work_queues/{id}", json=data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def get_runs_in_work_queue(
        self,
        id: UUID,
        limit: int = 10,
        scheduled_before: Optional[datetime.datetime] = None,
    ) -> list[FlowRun]:
        """
        Read flow runs off a work queue.

        Args:
            id: the id of the work queue to read from
            limit: a limit on the number of runs to return
            scheduled_before: a timestamp; only runs scheduled before this time will be returned.
                Defaults to now.

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If request fails

        Returns:
            List[FlowRun]: a list of FlowRun objects read from the queue
        """
        if scheduled_before is None:
            scheduled_before = pendulum.now("UTC")

        try:
            response = await self._client.post(
                f"/work_queues/{id}/get_runs",
                json={
                    "limit": limit,
                    "scheduled_before": scheduled_before.isoformat(),
                },
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return pydantic.TypeAdapter(list[FlowRun]).validate_python(response.json())

    async def read_work_queue(
        self,
        id: UUID,
    ) -> WorkQueue:
        """
        Read a work queue.

        Args:
            id: the id of the work queue to load

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If request fails

        Returns:
            WorkQueue: an instantiated WorkQueue object
        """
        try:
            response = await self._client.get(f"/work_queues/{id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return WorkQueue.model_validate(response.json())

    async def read_work_queue_status(
        self,
        id: UUID,
    ) -> WorkQueueStatusDetail:
        """
        Read a work queue status.

        Args:
            id: the id of the work queue to load

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If request fails

        Returns:
            WorkQueueStatus: an instantiated WorkQueueStatus object
        """
        try:
            response = await self._client.get(f"/work_queues/{id}/status")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return WorkQueueStatusDetail.model_validate(response.json())

    async def match_work_queues(
        self,
        prefixes: list[str],
        work_pool_name: Optional[str] = None,
    ) -> list[WorkQueue]:
        """
        Query the Prefect API for work queues with names with a specific prefix.

        Args:
            prefixes: a list of strings used to match work queue name prefixes
            work_pool_name: an optional work pool name to scope the query to

        Returns:
            a list of WorkQueue model representations
                of the work queues
        """
        page_length = 100
        current_page = 0
        work_queues: list[WorkQueue] = []

        while True:
            new_queues = await self.read_work_queues(
                work_pool_name=work_pool_name,
                offset=current_page * page_length,
                limit=page_length,
                work_queue_filter=WorkQueueFilter(
                    name=WorkQueueFilterName(startswith_=prefixes)
                ),
            )
            if not new_queues:
                break
            work_queues += new_queues
            current_page += 1

        return work_queues

    async def delete_work_queue_by_id(
        self,
        id: UUID,
    ) -> None:
        """
        Delete a work queue by its ID.

        Args:
            id: the id of the work queue to delete

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        try:
            await self._client.delete(
                f"/work_queues/{id}",
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def create_block_type(self, block_type: BlockTypeCreate) -> BlockType:
        """
        Create a block type in the Prefect API.
        """
        try:
            response = await self._client.post(
                "/block_types/",
                json=block_type.model_dump(
                    mode="json", exclude_unset=True, exclude={"id"}
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_409_CONFLICT:
                raise prefect.exceptions.ObjectAlreadyExists(http_exc=e) from e
            else:
                raise
        return BlockType.model_validate(response.json())

    async def create_block_schema(self, block_schema: BlockSchemaCreate) -> BlockSchema:
        """
        Create a block schema in the Prefect API.
        """
        try:
            response = await self._client.post(
                "/block_schemas/",
                json=block_schema.model_dump(
                    mode="json",
                    exclude_unset=True,
                    exclude={"id", "block_type", "checksum"},
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_409_CONFLICT:
                raise prefect.exceptions.ObjectAlreadyExists(http_exc=e) from e
            else:
                raise
        return BlockSchema.model_validate(response.json())

    async def create_block_document(
        self,
        block_document: Union[BlockDocument, BlockDocumentCreate],
        include_secrets: bool = True,
    ) -> BlockDocument:
        """
        Create a block document in the Prefect API. This data is used to configure a
        corresponding Block.

        Args:
            include_secrets (bool): whether to include secret values
                on the stored Block, corresponding to Pydantic's `SecretStr` and
                `SecretBytes` fields. Note Blocks may not work as expected if
                this is set to `False`.
        """
        block_document_data = block_document.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"id", "block_schema", "block_type"},
            context={"include_secrets": include_secrets},
            serialize_as_any=True,
        )
        try:
            response = await self._client.post(
                "/block_documents/",
                json=block_document_data,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_409_CONFLICT:
                raise prefect.exceptions.ObjectAlreadyExists(http_exc=e) from e
            else:
                raise
        return BlockDocument.model_validate(response.json())

    async def update_block_document(
        self,
        block_document_id: UUID,
        block_document: BlockDocumentUpdate,
    ) -> None:
        """
        Update a block document in the Prefect API.
        """
        try:
            await self._client.patch(
                f"/block_documents/{block_document_id}",
                json=block_document.model_dump(
                    mode="json",
                    exclude_unset=True,
                    include={"data", "merge_existing_data", "block_schema_id"},
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def delete_block_document(self, block_document_id: UUID) -> None:
        """
        Delete a block document.
        """
        try:
            await self._client.delete(f"/block_documents/{block_document_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_block_type_by_slug(self, slug: str) -> BlockType:
        """
        Read a block type by its slug.
        """
        try:
            response = await self._client.get(f"/block_types/slug/{slug}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return BlockType.model_validate(response.json())

    async def read_block_schema_by_checksum(
        self, checksum: str, version: Optional[str] = None
    ) -> BlockSchema:
        """
        Look up a block schema checksum
        """
        try:
            url = f"/block_schemas/checksum/{checksum}"
            if version is not None:
                url = f"{url}?version={version}"
            response = await self._client.get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return BlockSchema.model_validate(response.json())

    async def update_block_type(
        self, block_type_id: UUID, block_type: BlockTypeUpdate
    ) -> None:
        """
        Update a block document in the Prefect API.
        """
        try:
            await self._client.patch(
                f"/block_types/{block_type_id}",
                json=block_type.model_dump(
                    mode="json",
                    exclude_unset=True,
                    include=BlockTypeUpdate.updatable_fields(),
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def delete_block_type(self, block_type_id: UUID) -> None:
        """
        Delete a block type.
        """
        try:
            await self._client.delete(f"/block_types/{block_type_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            elif (
                e.response.status_code == status.HTTP_403_FORBIDDEN
                and e.response.json()["detail"]
                == "protected block types cannot be deleted."
            ):
                raise prefect.exceptions.ProtectedBlockError(
                    "Protected block types cannot be deleted."
                ) from e
            else:
                raise

    async def read_block_types(self) -> list[BlockType]:
        """
        Read all block types
        Raises:
            httpx.RequestError: if the block types were not found

        Returns:
            List of BlockTypes.
        """
        response = await self._client.post("/block_types/filter", json={})
        return pydantic.TypeAdapter(list[BlockType]).validate_python(response.json())

    async def read_block_schemas(self) -> list[BlockSchema]:
        """
        Read all block schemas
        Raises:
            httpx.RequestError: if a valid block schema was not found

        Returns:
            A BlockSchema.
        """
        response = await self._client.post("/block_schemas/filter", json={})
        return pydantic.TypeAdapter(list[BlockSchema]).validate_python(response.json())

    async def get_most_recent_block_schema_for_block_type(
        self,
        block_type_id: UUID,
    ) -> Optional[BlockSchema]:
        """
        Fetches the most recent block schema for a specified block type ID.

        Args:
            block_type_id: The ID of the block type.

        Raises:
            httpx.RequestError: If the request fails for any reason.

        Returns:
            The most recent block schema or None.
        """
        try:
            response = await self._client.post(
                "/block_schemas/filter",
                json={
                    "block_schemas": {"block_type_id": {"any_": [str(block_type_id)]}},
                    "limit": 1,
                },
            )
        except httpx.HTTPStatusError:
            raise
        return (
            BlockSchema.model_validate(response.json()[0]) if response.json() else None
        )

    async def read_block_document(
        self,
        block_document_id: UUID,
        include_secrets: bool = True,
    ) -> BlockDocument:
        """
        Read the block document with the specified ID.

        Args:
            block_document_id: the block document id
            include_secrets (bool): whether to include secret values
                on the Block, corresponding to Pydantic's `SecretStr` and
                `SecretBytes` fields. These fields are automatically obfuscated
                by Pydantic, but users can additionally choose not to receive
                their values from the API. Note that any business logic on the
                Block may not work if this is `False`.

        Raises:
            httpx.RequestError: if the block document was not found for any reason

        Returns:
            A block document or None.
        """
        assert (
            block_document_id is not None
        ), "Unexpected ID on block document. Was it persisted?"
        try:
            response = await self._client.get(
                f"/block_documents/{block_document_id}",
                params=dict(include_secrets=include_secrets),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return BlockDocument.model_validate(response.json())

    async def read_block_document_by_name(
        self,
        name: str,
        block_type_slug: str,
        include_secrets: bool = True,
    ) -> BlockDocument:
        """
        Read the block document with the specified name that corresponds to a
        specific block type name.

        Args:
            name: The block document name.
            block_type_slug: The block type slug.
            include_secrets (bool): whether to include secret values
                on the Block, corresponding to Pydantic's `SecretStr` and
                `SecretBytes` fields. These fields are automatically obfuscated
                by Pydantic, but users can additionally choose not to receive
                their values from the API. Note that any business logic on the
                Block may not work if this is `False`.

        Raises:
            httpx.RequestError: if the block document was not found for any reason

        Returns:
            A block document or None.
        """
        try:
            response = await self._client.get(
                f"/block_types/slug/{block_type_slug}/block_documents/name/{name}",
                params=dict(include_secrets=include_secrets),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return BlockDocument.model_validate(response.json())

    async def read_block_documents(
        self,
        block_schema_type: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        include_secrets: bool = True,
    ) -> list[BlockDocument]:
        """
        Read block documents

        Args:
            block_schema_type: an optional block schema type
            offset: an offset
            limit: the number of blocks to return
            include_secrets (bool): whether to include secret values
                on the Block, corresponding to Pydantic's `SecretStr` and
                `SecretBytes` fields. These fields are automatically obfuscated
                by Pydantic, but users can additionally choose not to receive
                their values from the API. Note that any business logic on the
                Block may not work if this is `False`.

        Returns:
            A list of block documents
        """
        response = await self._client.post(
            "/block_documents/filter",
            json=dict(
                block_schema_type=block_schema_type,
                offset=offset,
                limit=limit,
                include_secrets=include_secrets,
            ),
        )
        return pydantic.TypeAdapter(list[BlockDocument]).validate_python(
            response.json()
        )

    async def read_block_documents_by_type(
        self,
        block_type_slug: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        include_secrets: bool = True,
    ) -> list[BlockDocument]:
        """Retrieve block documents by block type slug.

        Args:
            block_type_slug: The block type slug.
            offset: an offset
            limit: the number of blocks to return
            include_secrets: whether to include secret values

        Returns:
            A list of block documents
        """
        response = await self._client.get(
            f"/block_types/slug/{block_type_slug}/block_documents",
            params=dict(
                offset=offset,
                limit=limit,
                include_secrets=include_secrets,
            ),
        )

        return pydantic.TypeAdapter(list[BlockDocument]).validate_python(
            response.json()
        )

    async def create_deployment(
        self,
        flow_id: UUID,
        name: str,
        version: Optional[str] = None,
        schedules: Optional[list[DeploymentScheduleCreate]] = None,
        concurrency_limit: Optional[int] = None,
        concurrency_options: Optional[ConcurrencyOptions] = None,
        parameters: Optional[dict[str, Any]] = None,
        description: Optional[str] = None,
        work_queue_name: Optional[str] = None,
        work_pool_name: Optional[str] = None,
        tags: Optional[list[str]] = None,
        storage_document_id: Optional[UUID] = None,
        path: Optional[str] = None,
        entrypoint: Optional[str] = None,
        infrastructure_document_id: Optional[UUID] = None,
        parameter_openapi_schema: Optional[dict[str, Any]] = None,
        paused: Optional[bool] = None,
        pull_steps: Optional[list[dict[str, Any]]] = None,
        enforce_parameter_schema: Optional[bool] = None,
        job_variables: Optional[dict[str, Any]] = None,
    ) -> UUID:
        """
        Create a deployment.

        Args:
            flow_id: the flow ID to create a deployment for
            name: the name of the deployment
            version: an optional version string for the deployment
            tags: an optional list of tags to apply to the deployment
            storage_document_id: an reference to the storage block document
                used for the deployed flow
            infrastructure_document_id: an reference to the infrastructure block document
                to use for this deployment
            job_variables: A dictionary of dot delimited infrastructure overrides that
                will be applied at runtime; for example `env.CONFIG_KEY=config_value` or
                `namespace='prefect'`. This argument was previously named `infra_overrides`.
                Both arguments are supported for backwards compatibility.

        Raises:
            httpx.RequestError: if the deployment was not created for any reason

        Returns:
            the ID of the deployment in the backend
        """

        if parameter_openapi_schema is None:
            parameter_openapi_schema = {}

        deployment_create = DeploymentCreate(
            flow_id=flow_id,
            name=name,
            version=version,
            parameters=dict(parameters or {}),
            tags=list(tags or []),
            work_queue_name=work_queue_name,
            description=description,
            storage_document_id=storage_document_id,
            path=path,
            entrypoint=entrypoint,
            infrastructure_document_id=infrastructure_document_id,
            job_variables=dict(job_variables or {}),
            parameter_openapi_schema=parameter_openapi_schema,
            paused=paused,
            schedules=schedules or [],
            concurrency_limit=concurrency_limit,
            concurrency_options=concurrency_options,
            pull_steps=pull_steps,
            enforce_parameter_schema=enforce_parameter_schema,
        )

        if work_pool_name is not None:
            deployment_create.work_pool_name = work_pool_name

        # Exclude newer fields that are not set to avoid compatibility issues
        exclude = {
            field
            for field in ["work_pool_name", "work_queue_name"]
            if field not in deployment_create.model_fields_set
        }

        if deployment_create.paused is None:
            exclude.add("paused")

        if deployment_create.pull_steps is None:
            exclude.add("pull_steps")

        if deployment_create.enforce_parameter_schema is None:
            exclude.add("enforce_parameter_schema")

        json = deployment_create.model_dump(mode="json", exclude=exclude)
        response = await self._client.post(
            "/deployments/",
            json=json,
        )
        deployment_id = response.json().get("id")
        if not deployment_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        return UUID(deployment_id)

    async def set_deployment_paused_state(
        self, deployment_id: UUID, paused: bool
    ) -> None:
        await self._client.patch(
            f"/deployments/{deployment_id}", json={"paused": paused}
        )

    async def update_deployment(
        self,
        deployment_id: UUID,
        deployment: DeploymentUpdate,
    ) -> None:
        await self._client.patch(
            f"/deployments/{deployment_id}",
            json=deployment.model_dump(mode="json", exclude_unset=True),
        )

    async def _create_deployment_from_schema(self, schema: DeploymentCreate) -> UUID:
        """
        Create a deployment from a prepared `DeploymentCreate` schema.
        """
        # TODO: We are likely to remove this method once we have considered the
        #       packaging interface for deployments further.
        response = await self._client.post(
            "/deployments/", json=schema.model_dump(mode="json")
        )
        deployment_id = response.json().get("id")
        if not deployment_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        return UUID(deployment_id)

    async def read_deployment(
        self,
        deployment_id: Union[UUID, str],
    ) -> DeploymentResponse:
        """
        Query the Prefect API for a deployment by id.

        Args:
            deployment_id: the deployment ID of interest

        Returns:
            a [Deployment model][prefect.client.schemas.objects.Deployment] representation of the deployment
        """
        if not isinstance(deployment_id, UUID):
            try:
                deployment_id = UUID(deployment_id)
            except ValueError:
                raise ValueError(f"Invalid deployment ID: {deployment_id}")

        try:
            response = await self._client.get(f"/deployments/{deployment_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return DeploymentResponse.model_validate(response.json())

    async def read_deployment_by_name(
        self,
        name: str,
    ) -> DeploymentResponse:
        """
        Query the Prefect API for a deployment by name.

        Args:
            name: A deployed flow's name: <FLOW_NAME>/<DEPLOYMENT_NAME>

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If request fails

        Returns:
            a Deployment model representation of the deployment
        """
        try:
            response = await self._client.get(f"/deployments/name/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                from prefect.utilities.text import fuzzy_match_string

                deployments = await self.read_deployments()
                flow_name_map = {
                    flow.id: flow.name
                    for flow in await asyncio.gather(
                        *[
                            self.read_flow(flow_id)
                            for flow_id in {d.flow_id for d in deployments}
                        ]
                    )
                }

                raise prefect.exceptions.ObjectNotFound(
                    http_exc=e,
                    help_message=(
                        f"Deployment {name!r} not found; did you mean {fuzzy_match!r}?"
                        if (
                            fuzzy_match := fuzzy_match_string(
                                name,
                                [
                                    f"{flow_name_map[d.flow_id]}/{d.name}"
                                    for d in deployments
                                ],
                            )
                        )
                        else f"Deployment {name!r} not found. Try `prefect deployment ls` to find available deployments."
                    ),
                ) from e
            else:
                raise

        return DeploymentResponse.model_validate(response.json())

    async def read_deployments(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        work_pool_filter: Optional[WorkPoolFilter] = None,
        work_queue_filter: Optional[WorkQueueFilter] = None,
        limit: Optional[int] = None,
        sort: Optional[DeploymentSort] = None,
        offset: int = 0,
    ) -> list[DeploymentResponse]:
        """
        Query the Prefect API for deployments. Only deployments matching all
        the provided criteria will be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            work_pool_filter: filter criteria for work pools
            work_queue_filter: filter criteria for work pool queues
            limit: a limit for the deployment query
            offset: an offset for the deployment query

        Returns:
            a list of Deployment model representations
                of the deployments
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "work_pools": (
                work_pool_filter.model_dump(mode="json") if work_pool_filter else None
            ),
            "work_pool_queues": (
                work_queue_filter.model_dump(mode="json") if work_queue_filter else None
            ),
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }

        response = await self._client.post("/deployments/filter", json=body)
        return pydantic.TypeAdapter(list[DeploymentResponse]).validate_python(
            response.json()
        )

    async def delete_deployment(
        self,
        deployment_id: UUID,
    ) -> None:
        """
        Delete deployment by id.

        Args:
            deployment_id: The deployment id of interest.
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        try:
            await self._client.delete(f"/deployments/{deployment_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def create_deployment_schedules(
        self,
        deployment_id: UUID,
        schedules: list[tuple[SCHEDULE_TYPES, bool]],
    ) -> list[DeploymentSchedule]:
        """
        Create deployment schedules.

        Args:
            deployment_id: the deployment ID
            schedules: a list of tuples containing the schedule to create
                       and whether or not it should be active.

        Raises:
            httpx.RequestError: if the schedules were not created for any reason

        Returns:
            the list of schedules created in the backend
        """
        deployment_schedule_create = [
            DeploymentScheduleCreate(schedule=schedule[0], active=schedule[1])
            for schedule in schedules
        ]

        json = [
            deployment_schedule_create.model_dump(mode="json")
            for deployment_schedule_create in deployment_schedule_create
        ]
        response = await self._client.post(
            f"/deployments/{deployment_id}/schedules", json=json
        )
        return pydantic.TypeAdapter(list[DeploymentSchedule]).validate_python(
            response.json()
        )

    async def read_deployment_schedules(
        self,
        deployment_id: UUID,
    ) -> list[DeploymentSchedule]:
        """
        Query the Prefect API for a deployment's schedules.

        Args:
            deployment_id: the deployment ID

        Returns:
            a list of DeploymentSchedule model representations of the deployment schedules
        """
        try:
            response = await self._client.get(f"/deployments/{deployment_id}/schedules")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return pydantic.TypeAdapter(list[DeploymentSchedule]).validate_python(
            response.json()
        )

    async def update_deployment_schedule(
        self,
        deployment_id: UUID,
        schedule_id: UUID,
        active: Optional[bool] = None,
        schedule: Optional[SCHEDULE_TYPES] = None,
    ) -> None:
        """
        Update a deployment schedule by ID.

        Args:
            deployment_id: the deployment ID
            schedule_id: the deployment schedule ID of interest
            active: whether or not the schedule should be active
            schedule: the cron, rrule, or interval schedule this deployment schedule should use
        """
        kwargs: dict[str, Any] = {}
        if active is not None:
            kwargs["active"] = active
        if schedule is not None:
            kwargs["schedule"] = schedule

        deployment_schedule_update = DeploymentScheduleUpdate(**kwargs)
        json = deployment_schedule_update.model_dump(mode="json", exclude_unset=True)

        try:
            await self._client.patch(
                f"/deployments/{deployment_id}/schedules/{schedule_id}", json=json
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def delete_deployment_schedule(
        self,
        deployment_id: UUID,
        schedule_id: UUID,
    ) -> None:
        """
        Delete a deployment schedule.

        Args:
            deployment_id: the deployment ID
            schedule_id: the ID of the deployment schedule to delete.

        Raises:
            httpx.RequestError: if the schedules were not deleted for any reason
        """
        try:
            await self._client.delete(
                f"/deployments/{deployment_id}/schedules/{schedule_id}"
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_flow_run(self, flow_run_id: UUID) -> FlowRun:
        """
        Query the Prefect API for a flow run by id.

        Args:
            flow_run_id: the flow run ID of interest

        Returns:
            a Flow Run model representation of the flow run
        """
        try:
            response = await self._client.get(f"/flow_runs/{flow_run_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return FlowRun.model_validate(response.json())

    async def resume_flow_run(
        self, flow_run_id: UUID, run_input: Optional[dict[str, Any]] = None
    ) -> OrchestrationResult[Any]:
        """
        Resumes a paused flow run.

        Args:
            flow_run_id: the flow run ID of interest
            run_input: the input to resume the flow run with

        Returns:
            an OrchestrationResult model representation of state orchestration output
        """
        try:
            response = await self._client.post(
                f"/flow_runs/{flow_run_id}/resume", json={"run_input": run_input}
            )
        except httpx.HTTPStatusError:
            raise

        result: OrchestrationResult[Any] = OrchestrationResult.model_validate(
            response.json()
        )
        return result

    async def read_flow_runs(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        work_pool_filter: Optional[WorkPoolFilter] = None,
        work_queue_filter: Optional[WorkQueueFilter] = None,
        sort: Optional[FlowRunSort] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[FlowRun]:
        """
        Query the Prefect API for flow runs. Only flow runs matching all criteria will
        be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            work_pool_filter: filter criteria for work pools
            work_queue_filter: filter criteria for work pool queues
            sort: sort criteria for the flow runs
            limit: limit for the flow run query
            offset: offset for the flow run query

        Returns:
            a list of Flow Run model representations
                of the flow runs
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "work_pools": (
                work_pool_filter.model_dump(mode="json") if work_pool_filter else None
            ),
            "work_pool_queues": (
                work_queue_filter.model_dump(mode="json") if work_queue_filter else None
            ),
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }

        response = await self._client.post("/flow_runs/filter", json=body)
        return pydantic.TypeAdapter(list[FlowRun]).validate_python(response.json())

    async def set_flow_run_state(
        self,
        flow_run_id: Union[UUID, str],
        state: "prefect.states.State[T]",
        force: bool = False,
    ) -> OrchestrationResult[T]:
        """
        Set the state of a flow run.

        Args:
            flow_run_id: the id of the flow run
            state: the state to set
            force: if True, disregard orchestration logic when setting the state,
                forcing the Prefect API to accept the state

        Returns:
            an OrchestrationResult model representation of state orchestration output
        """
        flow_run_id = (
            flow_run_id if isinstance(flow_run_id, UUID) else UUID(flow_run_id)
        )
        state_create = state.to_state_create()
        state_create.state_details.flow_run_id = flow_run_id
        state_create.state_details.transition_id = uuid4()
        try:
            response = await self._client.post(
                f"/flow_runs/{flow_run_id}/set_state",
                json=dict(
                    state=state_create.model_dump(mode="json", serialize_as_any=True),
                    force=force,
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

        result: OrchestrationResult[T] = OrchestrationResult.model_validate(
            response.json()
        )
        return result

    async def read_flow_run_states(
        self, flow_run_id: UUID
    ) -> list[prefect.states.State]:
        """
        Query for the states of a flow run

        Args:
            flow_run_id: the id of the flow run

        Returns:
            a list of State model representations
                of the flow run states
        """
        response = await self._client.get(
            "/flow_run_states/", params=dict(flow_run_id=str(flow_run_id))
        )
        return pydantic.TypeAdapter(list[prefect.states.State]).validate_python(
            response.json()
        )

    async def set_flow_run_name(self, flow_run_id: UUID, name: str) -> httpx.Response:
        flow_run_data = FlowRunUpdate(name=name)
        return await self._client.patch(
            f"/flow_runs/{flow_run_id}",
            json=flow_run_data.model_dump(mode="json", exclude_unset=True),
        )

    async def set_task_run_name(self, task_run_id: UUID, name: str) -> httpx.Response:
        task_run_data = TaskRunUpdate(name=name)
        return await self._client.patch(
            f"/task_runs/{task_run_id}",
            json=task_run_data.model_dump(mode="json", exclude_unset=True),
        )

    async def create_task_run(
        self,
        task: "TaskObject[P, R]",
        flow_run_id: Optional[UUID],
        dynamic_key: str,
        id: Optional[UUID] = None,
        name: Optional[str] = None,
        extra_tags: Optional[Iterable[str]] = None,
        state: Optional[prefect.states.State[R]] = None,
        task_inputs: Optional[
            dict[
                str,
                list[
                    Union[
                        TaskRunResult,
                        Parameter,
                        Constant,
                    ]
                ],
            ]
        ] = None,
    ) -> TaskRun:
        """
        Create a task run

        Args:
            task: The Task to run
            flow_run_id: The flow run id with which to associate the task run
            dynamic_key: A key unique to this particular run of a Task within the flow
            id: An optional ID for the task run. If not provided, one will be generated
                server-side.
            name: An optional name for the task run
            extra_tags: an optional list of extra tags to apply to the task run in
                addition to `task.tags`
            state: The initial state for the run. If not provided, defaults to
                `Pending` for now. Should always be a `Scheduled` type.
            task_inputs: the set of inputs passed to the task

        Returns:
            The created task run.
        """
        tags = set(task.tags).union(extra_tags or [])

        if state is None:
            state = prefect.states.Pending()

        retry_delay = task.retry_delay_seconds
        if isinstance(retry_delay, list):
            retry_delay = [int(rd) for rd in retry_delay]
        elif isinstance(retry_delay, float):
            retry_delay = int(retry_delay)

        task_run_data = TaskRunCreate(
            id=id,
            name=name,
            flow_run_id=flow_run_id,
            task_key=task.task_key,
            dynamic_key=str(dynamic_key),
            tags=list(tags),
            task_version=task.version,
            empirical_policy=TaskRunPolicy(
                retries=task.retries,
                retry_delay=retry_delay,
                retry_jitter_factor=task.retry_jitter_factor,
            ),
            state=state.to_state_create(),
            task_inputs=task_inputs or {},
        )
        content = task_run_data.model_dump_json(exclude={"id"} if id is None else None)

        response = await self._client.post("/task_runs/", content=content)
        return TaskRun.model_validate(response.json())

    async def read_task_run(self, task_run_id: UUID) -> TaskRun:
        """
        Query the Prefect API for a task run by id.

        Args:
            task_run_id: the task run ID of interest

        Returns:
            a Task Run model representation of the task run
        """
        try:
            response = await self._client.get(f"/task_runs/{task_run_id}")
            return TaskRun.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_task_runs(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        sort: Optional[TaskRunSort] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[TaskRun]:
        """
        Query the Prefect API for task runs. Only task runs matching all criteria will
        be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            sort: sort criteria for the task runs
            limit: a limit for the task run query
            offset: an offset for the task run query

        Returns:
            a list of Task Run model representations
                of the task runs
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }
        response = await self._client.post("/task_runs/filter", json=body)
        return pydantic.TypeAdapter(list[TaskRun]).validate_python(response.json())

    async def delete_task_run(self, task_run_id: UUID) -> None:
        """
        Delete a task run by id.

        Args:
            task_run_id: the task run ID of interest
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        try:
            await self._client.delete(f"/task_runs/{task_run_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def set_task_run_state(
        self,
        task_run_id: UUID,
        state: prefect.states.State[T],
        force: bool = False,
    ) -> OrchestrationResult[T]:
        """
        Set the state of a task run.

        Args:
            task_run_id: the id of the task run
            state: the state to set
            force: if True, disregard orchestration logic when setting the state,
                forcing the Prefect API to accept the state

        Returns:
            an OrchestrationResult model representation of state orchestration output
        """
        state_create = state.to_state_create()
        state_create.state_details.task_run_id = task_run_id
        response = await self._client.post(
            f"/task_runs/{task_run_id}/set_state",
            json=dict(state=state_create.model_dump(mode="json"), force=force),
        )
        result: OrchestrationResult[T] = OrchestrationResult.model_validate(
            response.json()
        )
        return result

    async def read_task_run_states(
        self, task_run_id: UUID
    ) -> list[prefect.states.State]:
        """
        Query for the states of a task run

        Args:
            task_run_id: the id of the task run

        Returns:
            a list of State model representations of the task run states
        """
        response = await self._client.get(
            "/task_run_states/", params=dict(task_run_id=str(task_run_id))
        )
        return pydantic.TypeAdapter(list[prefect.states.State]).validate_python(
            response.json()
        )

    async def create_flow_run_notification_policy(
        self,
        block_document_id: UUID,
        is_active: bool = True,
        tags: Optional[list[str]] = None,
        state_names: Optional[list[str]] = None,
        message_template: Optional[str] = None,
    ) -> UUID:
        """
        Create a notification policy for flow runs

        Args:
            block_document_id: The block document UUID
            is_active: Whether the notification policy is active
            tags: List of flow tags
            state_names: List of state names
            message_template: Notification message template
        """
        if tags is None:
            tags = []
        if state_names is None:
            state_names = []

        policy = FlowRunNotificationPolicyCreate(
            block_document_id=block_document_id,
            is_active=is_active,
            tags=tags,
            state_names=state_names,
            message_template=message_template,
        )
        response = await self._client.post(
            "/flow_run_notification_policies/",
            json=policy.model_dump(mode="json"),
        )

        policy_id = response.json().get("id")
        if not policy_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        return UUID(policy_id)

    async def delete_flow_run_notification_policy(
        self,
        id: UUID,
    ) -> None:
        """
        Delete a flow run notification policy by id.

        Args:
            id: UUID of the flow run notification policy to delete.
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        try:
            await self._client.delete(f"/flow_run_notification_policies/{id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def update_flow_run_notification_policy(
        self,
        id: UUID,
        block_document_id: Optional[UUID] = None,
        is_active: Optional[bool] = None,
        tags: Optional[list[str]] = None,
        state_names: Optional[list[str]] = None,
        message_template: Optional[str] = None,
    ) -> None:
        """
        Update a notification policy for flow runs

        Args:
            id: UUID of the notification policy
            block_document_id: The block document UUID
            is_active: Whether the notification policy is active
            tags: List of flow tags
            state_names: List of state names
            message_template: Notification message template
        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If requests fails
        """
        params: dict[str, Any] = {}
        if block_document_id is not None:
            params["block_document_id"] = block_document_id
        if is_active is not None:
            params["is_active"] = is_active
        if tags is not None:
            params["tags"] = tags
        if state_names is not None:
            params["state_names"] = state_names
        if message_template is not None:
            params["message_template"] = message_template

        policy = FlowRunNotificationPolicyUpdate(**params)

        try:
            await self._client.patch(
                f"/flow_run_notification_policies/{id}",
                json=policy.model_dump(mode="json", exclude_unset=True),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_flow_run_notification_policies(
        self,
        flow_run_notification_policy_filter: FlowRunNotificationPolicyFilter,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[FlowRunNotificationPolicy]:
        """
        Query the Prefect API for flow run notification policies. Only policies matching all criteria will
        be returned.

        Args:
            flow_run_notification_policy_filter: filter criteria for notification policies
            limit: a limit for the notification policies query
            offset: an offset for the notification policies query

        Returns:
            a list of FlowRunNotificationPolicy model representations
                of the notification policies
        """
        body: dict[str, Any] = {
            "flow_run_notification_policy_filter": (
                flow_run_notification_policy_filter.model_dump(mode="json")
                if flow_run_notification_policy_filter
                else None
            ),
            "limit": limit,
            "offset": offset,
        }
        response = await self._client.post(
            "/flow_run_notification_policies/filter", json=body
        )
        return pydantic.TypeAdapter(list[FlowRunNotificationPolicy]).validate_python(
            response.json()
        )

    async def send_worker_heartbeat(
        self,
        work_pool_name: str,
        worker_name: str,
        heartbeat_interval_seconds: Optional[float] = None,
        get_worker_id: bool = False,
        worker_metadata: Optional[WorkerMetadata] = None,
    ) -> Optional[UUID]:
        """
        Sends a worker heartbeat for a given work pool.

        Args:
            work_pool_name: The name of the work pool to heartbeat against.
            worker_name: The name of the worker sending the heartbeat.
            return_id: Whether to return the worker ID. Note: will return `None` if the connected server does not support returning worker IDs, even if `return_id` is `True`.
            worker_metadata: Metadata about the worker to send to the server.
        """
        params: dict[str, Any] = {
            "name": worker_name,
            "heartbeat_interval_seconds": heartbeat_interval_seconds,
        }
        if worker_metadata:
            params["metadata"] = worker_metadata.model_dump(mode="json")
        if get_worker_id:
            params["return_id"] = get_worker_id

        resp = await self._client.post(
            f"/work_pools/{work_pool_name}/workers/heartbeat",
            json=params,
        )

        if (
            (
                self.server_type == ServerType.CLOUD
                or get_current_settings().testing.test_mode
            )
            and get_worker_id
            and resp.status_code == 200
        ):
            return UUID(resp.text)
        else:
            return None

    async def read_workers_for_work_pool(
        self,
        work_pool_name: str,
        worker_filter: Optional[WorkerFilter] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Worker]:
        """
        Reads workers for a given work pool.

        Args:
            work_pool_name: The name of the work pool for which to get
                member workers.
            worker_filter: Criteria by which to filter workers.
            limit: Limit for the worker query.
            offset: Limit for the worker query.
        """
        response = await self._client.post(
            f"/work_pools/{work_pool_name}/workers/filter",
            json={
                "workers": (
                    worker_filter.model_dump(mode="json", exclude_unset=True)
                    if worker_filter
                    else None
                ),
                "offset": offset,
                "limit": limit,
            },
        )

        return pydantic.TypeAdapter(list[Worker]).validate_python(response.json())

    async def read_work_pool(self, work_pool_name: str) -> WorkPool:
        """
        Reads information for a given work pool

        Args:
            work_pool_name: The name of the work pool to for which to get
                information.

        Returns:
            Information about the requested work pool.
        """
        try:
            response = await self._client.get(f"/work_pools/{work_pool_name}")
            return WorkPool.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_work_pools(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
        work_pool_filter: Optional[WorkPoolFilter] = None,
    ) -> list[WorkPool]:
        """
        Reads work pools.

        Args:
            limit: Limit for the work pool query.
            offset: Offset for the work pool query.
            work_pool_filter: Criteria by which to filter work pools.

        Returns:
            A list of work pools.
        """

        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "work_pools": (
                work_pool_filter.model_dump(mode="json") if work_pool_filter else None
            ),
        }
        response = await self._client.post("/work_pools/filter", json=body)
        return pydantic.TypeAdapter(list[WorkPool]).validate_python(response.json())

    async def create_work_pool(
        self,
        work_pool: WorkPoolCreate,
        overwrite: bool = False,
    ) -> WorkPool:
        """
        Creates a work pool with the provided configuration.

        Args:
            work_pool: Desired configuration for the new work pool.

        Returns:
            Information about the newly created work pool.
        """
        try:
            response = await self._client.post(
                "/work_pools/",
                json=work_pool.model_dump(mode="json", exclude_unset=True),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_409_CONFLICT:
                if overwrite:
                    existing_work_pool = await self.read_work_pool(
                        work_pool_name=work_pool.name
                    )
                    if existing_work_pool.type != work_pool.type:
                        warnings.warn(
                            "Overwriting work pool type is not supported. Ignoring provided type.",
                            category=UserWarning,
                        )
                    await self.update_work_pool(
                        work_pool_name=work_pool.name,
                        work_pool=WorkPoolUpdate.model_validate(
                            work_pool.model_dump(exclude={"name", "type"})
                        ),
                    )
                    response = await self._client.get(f"/work_pools/{work_pool.name}")
                else:
                    raise prefect.exceptions.ObjectAlreadyExists(http_exc=e) from e
            else:
                raise

        return WorkPool.model_validate(response.json())

    async def update_work_pool(
        self,
        work_pool_name: str,
        work_pool: WorkPoolUpdate,
    ) -> None:
        """
        Updates a work pool.

        Args:
            work_pool_name: Name of the work pool to update.
            work_pool: Fields to update in the work pool.
        """
        try:
            await self._client.patch(
                f"/work_pools/{work_pool_name}",
                json=work_pool.model_dump(mode="json", exclude_unset=True),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def delete_work_pool(
        self,
        work_pool_name: str,
    ) -> None:
        """
        Deletes a work pool.

        Args:
            work_pool_name: Name of the work pool to delete.
        """
        try:
            await self._client.delete(f"/work_pools/{work_pool_name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    async def read_work_queues(
        self,
        work_pool_name: Optional[str] = None,
        work_queue_filter: Optional[WorkQueueFilter] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> list[WorkQueue]:
        """
        Retrieves queues for a work pool.

        Args:
            work_pool_name: Name of the work pool for which to get queues.
            work_queue_filter: Criteria by which to filter queues.
            limit: Limit for the queue query.
            offset: Limit for the queue query.

        Returns:
            List of queues for the specified work pool.
        """
        json: dict[str, Any] = {
            "work_queues": (
                work_queue_filter.model_dump(mode="json", exclude_unset=True)
                if work_queue_filter
                else None
            ),
            "limit": limit,
            "offset": offset,
        }

        if work_pool_name:
            try:
                response = await self._client.post(
                    f"/work_pools/{work_pool_name}/queues/filter",
                    json=json,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == status.HTTP_404_NOT_FOUND:
                    raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
                else:
                    raise
        else:
            response = await self._client.post("/work_queues/filter", json=json)

        return pydantic.TypeAdapter(list[WorkQueue]).validate_python(response.json())

    async def get_scheduled_flow_runs_for_deployments(
        self,
        deployment_ids: list[UUID],
        scheduled_before: Optional[datetime.datetime] = None,
        limit: Optional[int] = None,
    ) -> list[FlowRunResponse]:
        body: dict[str, Any] = dict(deployment_ids=[str(id) for id in deployment_ids])
        if scheduled_before:
            body["scheduled_before"] = str(scheduled_before)
        if limit:
            body["limit"] = limit

        response = await self._client.post(
            "/deployments/get_scheduled_flow_runs",
            json=body,
        )

        return pydantic.TypeAdapter(list[FlowRunResponse]).validate_python(
            response.json()
        )

    async def get_scheduled_flow_runs_for_work_pool(
        self,
        work_pool_name: str,
        work_queue_names: Optional[list[str]] = None,
        scheduled_before: Optional[datetime.datetime] = None,
    ) -> list[WorkerFlowRunResponse]:
        """
        Retrieves scheduled flow runs for the provided set of work pool queues.

        Args:
            work_pool_name: The name of the work pool that the work pool
                queues are associated with.
            work_queue_names: The names of the work pool queues from which
                to get scheduled flow runs.
            scheduled_before: Datetime used to filter returned flow runs. Flow runs
                scheduled for after the given datetime string will not be returned.

        Returns:
            A list of worker flow run responses containing information about the
            retrieved flow runs.
        """
        body: dict[str, Any] = {}
        if work_queue_names is not None:
            body["work_queue_names"] = list(work_queue_names)
        if scheduled_before:
            body["scheduled_before"] = str(scheduled_before)

        response = await self._client.post(
            f"/work_pools/{work_pool_name}/get_scheduled_flow_runs",
            json=body,
        )
        return pydantic.TypeAdapter(list[WorkerFlowRunResponse]).validate_python(
            response.json()
        )

    async def read_worker_metadata(self) -> dict[str, Any]:
        """Reads worker metadata stored in Prefect collection registry."""
        response = await self._client.get("collections/views/aggregate-worker-metadata")
        response.raise_for_status()
        return response.json()

    async def create_flow_run_input(
        self, flow_run_id: UUID, key: str, value: str, sender: Optional[str] = None
    ) -> None:
        """
        Creates a flow run input.

        Args:
            flow_run_id: The flow run id.
            key: The input key.
            value: The input value.
            sender: The sender of the input.
        """

        # Initialize the input to ensure that the key is valid.
        FlowRunInput(flow_run_id=flow_run_id, key=key, value=value)

        response = await self._client.post(
            f"/flow_runs/{flow_run_id}/input",
            json={"key": key, "value": value, "sender": sender},
        )
        response.raise_for_status()

    async def filter_flow_run_input(
        self, flow_run_id: UUID, key_prefix: str, limit: int, exclude_keys: set[str]
    ) -> list[FlowRunInput]:
        response = await self._client.post(
            f"/flow_runs/{flow_run_id}/input/filter",
            json={
                "prefix": key_prefix,
                "limit": limit,
                "exclude_keys": list(exclude_keys),
            },
        )
        response.raise_for_status()
        return pydantic.TypeAdapter(list[FlowRunInput]).validate_python(response.json())

    async def read_flow_run_input(self, flow_run_id: UUID, key: str) -> str:
        """
        Reads a flow run input.

        Args:
            flow_run_id: The flow run id.
            key: The input key.
        """
        response = await self._client.get(f"/flow_runs/{flow_run_id}/input/{key}")
        response.raise_for_status()
        return response.content.decode()

    async def delete_flow_run_input(self, flow_run_id: UUID, key: str) -> None:
        """
        Deletes a flow run input.

        Args:
            flow_run_id: The flow run id.
            key: The input key.
        """
        response = await self._client.delete(f"/flow_runs/{flow_run_id}/input/{key}")
        response.raise_for_status()

    async def create_automation(self, automation: AutomationCore) -> UUID:
        """Creates an automation in Prefect Cloud."""
        response = await self._client.post(
            "/automations/",
            json=automation.model_dump(mode="json"),
        )

        return UUID(response.json()["id"])

    async def update_automation(
        self, automation_id: UUID, automation: AutomationCore
    ) -> None:
        """Updates an automation in Prefect Cloud."""
        response = await self._client.put(
            f"/automations/{automation_id}",
            json=automation.model_dump(mode="json", exclude_unset=True),
        )
        response.raise_for_status

    async def read_automations(self) -> list[Automation]:
        response = await self._client.post("/automations/filter")
        response.raise_for_status()
        return pydantic.TypeAdapter(list[Automation]).validate_python(response.json())

    async def find_automation(
        self, id_or_name: Union[str, UUID]
    ) -> Optional[Automation]:
        if isinstance(id_or_name, str):
            name = id_or_name
            try:
                id = UUID(id_or_name)
            except ValueError:
                id = None
        else:
            id = id_or_name
            name = str(id)

        if id:
            try:
                automation = await self.read_automation(id)
                return automation
            except prefect.exceptions.HTTPStatusError as e:
                if e.response.status_code == status.HTTP_404_NOT_FOUND:
                    raise prefect.exceptions.ObjectNotFound(http_exc=e) from e

        automations = await self.read_automations()

        # Look for it by an exact name
        for automation in automations:
            if automation.name == name:
                return automation

        # Look for it by a case-insensitive name
        for automation in automations:
            if automation.name.lower() == name.lower():
                return automation

        return None

    async def read_automation(
        self, automation_id: Union[UUID, str]
    ) -> Optional[Automation]:
        response = await self._client.get(f"/automations/{automation_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return Automation.model_validate(response.json())

    async def read_automations_by_name(self, name: str) -> list[Automation]:
        """
        Query the Prefect API for an automation by name. Only automations matching the provided name will be returned.

        Args:
            name: the name of the automation to query

        Returns:
            a list of Automation model representations of the automations
        """
        automation_filter = filters.AutomationFilter(
            name=filters.AutomationFilterName(any_=[name])
        )

        response = await self._client.post(
            "/automations/filter",
            json={
                "sort": sorting.AutomationSort.UPDATED_DESC,
                "automations": automation_filter.model_dump(mode="json")
                if automation_filter
                else None,
            },
        )

        response.raise_for_status()

        return pydantic.TypeAdapter(list[Automation]).validate_python(response.json())

    async def pause_automation(self, automation_id: UUID) -> None:
        response = await self._client.patch(
            f"/automations/{automation_id}", json={"enabled": False}
        )
        response.raise_for_status()

    async def resume_automation(self, automation_id: UUID) -> None:
        response = await self._client.patch(
            f"/automations/{automation_id}", json={"enabled": True}
        )
        response.raise_for_status()

    async def delete_automation(self, automation_id: UUID) -> None:
        response = await self._client.delete(f"/automations/{automation_id}")
        if response.status_code == 404:
            return

        response.raise_for_status()

    async def read_resource_related_automations(
        self, resource_id: str
    ) -> list[Automation]:
        response = await self._client.get(f"/automations/related-to/{resource_id}")
        response.raise_for_status()
        return pydantic.TypeAdapter(list[Automation]).validate_python(response.json())

    async def delete_resource_owned_automations(self, resource_id: str) -> None:
        await self._client.delete(f"/automations/owned-by/{resource_id}")

    async def api_version(self) -> str:
        res = await self._client.get("/admin/version")
        return res.json()

    def client_version(self) -> str:
        return prefect.__version__

    async def raise_for_api_version_mismatch(self) -> None:
        # Cloud is always compatible as a server
        if self.server_type == ServerType.CLOUD:
            return

        try:
            api_version = await self.api_version()
        except Exception as e:
            if "Unauthorized" in str(e):
                raise e
            raise RuntimeError(f"Failed to reach API at {self.api_url}") from e

        api_version = version.parse(api_version)
        client_version = version.parse(self.client_version())

        if api_version.major != client_version.major:
            raise RuntimeError(
                f"Found incompatible versions: client: {client_version}, server: {api_version}. "
                f"Major versions must match."
            )

    async def update_flow_run_labels(
        self, flow_run_id: UUID, labels: KeyValueLabelsField
    ) -> None:
        """
        Updates the labels of a flow run.
        """

        response = await self._client.patch(
            f"/flow_runs/{flow_run_id}/labels", json=labels
        )
        response.raise_for_status()

    async def create_sla(self, sla: SlaTypes) -> UUID:
        """
        Creates a service level agreement.

        Args:
            sla: The SLA to create. Must have a deployment ID set.

        Raises:
            httpx.RequestError: if the SLA was not created for any reason

        Returns:
            the ID of the SLA in the backend
        """
        if not sla.owner_resource:
            raise ValueError(
                "Deployment ID is not set. Please set using `set_deployment_id`."
            )

        response = await self._client.post(
            "/slas/",
            json=sla.model_dump(mode="json", exclude_unset=True),
        )
        response.raise_for_status()

        sla_id = response.json().get("id")
        if not sla_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        return UUID(sla_id)

    async def __aenter__(self) -> Self:
        """
        Start the client.

        If the client is already started, this will raise an exception.

        If the client is already closed, this will raise an exception. Use a new client
        instance instead.
        """
        if self._closed:
            # httpx.AsyncClient does not allow reuse so we will not either.
            raise RuntimeError(
                "The client cannot be started again after closing. "
                "Retrieve a new client with `get_client()` instead."
            )

        self._context_stack += 1

        if self._started:
            # allow reentrancy
            return self

        self._loop = asyncio.get_running_loop()
        await self._exit_stack.__aenter__()

        # Enter a lifespan context if using an ephemeral application.
        # See https://github.com/encode/httpx/issues/350
        if self._ephemeral_app and self.manage_lifespan:
            self._ephemeral_lifespan = await self._exit_stack.enter_async_context(
                app_lifespan_context(self._ephemeral_app)
            )

        if self._ephemeral_app:
            self.logger.debug(
                "Using ephemeral application with database at "
                f"{PREFECT_API_DATABASE_CONNECTION_URL.value()}"
            )
        else:
            self.logger.debug(f"Connecting to API at {self.api_url}")

        # Enter the httpx client's context
        await self._exit_stack.enter_async_context(self._client)

        self._started = True

        return self

    async def __aexit__(self, *exc_info: Any) -> Optional[bool]:
        """
        Shutdown the client.
        """

        self._context_stack -= 1
        if self._context_stack > 0:
            return
        self._closed = True
        return await self._exit_stack.__aexit__(*exc_info)

    def __enter__(self) -> NoReturn:
        raise RuntimeError(
            "The `PrefectClient` must be entered with an async context. Use 'async "
            "with PrefectClient(...)' not 'with PrefectClient(...)'"
        )

    def __exit__(self, *_: object) -> NoReturn:
        assert False, "This should never be called but must be defined for __enter__"


class SyncPrefectClient(
    ArtifactClient,
    ArtifactCollectionClient,
    LogClient,
    VariableClient,
    ConcurrencyLimitClient,
):
    """
    A synchronous client for interacting with the [Prefect REST API](/api-ref/rest-api/).

    Args:
        api: the REST API URL or FastAPI application to connect to
        api_key: An optional API key for authentication.
        api_version: The API version this client is compatible with.
        httpx_settings: An optional dictionary of settings to pass to the underlying
            `httpx.Client`

    Examples:

        Say hello to a Prefect REST API

        <div class="terminal">
        ```
        >>> with get_client(sync_client=True) as client:
        >>>     response = client.hello()
        >>>
        >>> print(response.json())
        👋
        ```
        </div>
    """

    def __init__(
        self,
        api: Union[str, ASGIApp],
        *,
        auth_string: Optional[str] = None,
        api_key: Optional[str] = None,
        api_version: Optional[str] = None,
        httpx_settings: Optional[dict[str, Any]] = None,
        server_type: Optional[ServerType] = None,
    ) -> None:
        httpx_settings = httpx_settings.copy() if httpx_settings else {}
        httpx_settings.setdefault("headers", {})

        if PREFECT_API_TLS_INSECURE_SKIP_VERIFY:
            # Create an unverified context for insecure connections
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            httpx_settings.setdefault("verify", ctx)
        else:
            cert_file = PREFECT_API_SSL_CERT_FILE.value()
            if not cert_file:
                cert_file = certifi.where()
            # Create a verified context with the certificate file
            ctx = ssl.create_default_context(cafile=cert_file)
            httpx_settings.setdefault("verify", ctx)

        if api_version is None:
            api_version = SERVER_API_VERSION
        httpx_settings["headers"].setdefault("X-PREFECT-API-VERSION", api_version)
        if api_key:
            httpx_settings["headers"].setdefault("Authorization", f"Bearer {api_key}")

        if auth_string:
            token = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
            httpx_settings["headers"].setdefault("Authorization", f"Basic {token}")

        # Context management
        self._context_stack: int = 0
        self._ephemeral_app: Optional[ASGIApp] = None
        self.manage_lifespan = True
        self.server_type: ServerType

        self._closed = False
        self._started = False

        # Connect to an external application
        if isinstance(api, str):
            if httpx_settings.get("app"):
                raise ValueError(
                    "Invalid httpx settings: `app` cannot be set when providing an "
                    "api url. `app` is only for use with ephemeral instances. Provide "
                    "it as the `api` parameter instead."
                )
            httpx_settings.setdefault("base_url", api)

            # See https://www.python-httpx.org/advanced/#pool-limit-configuration
            httpx_settings.setdefault(
                "limits",
                httpx.Limits(
                    # We see instability when allowing the client to open many connections at once.
                    # Limiting concurrency results in more stable performance.
                    max_connections=16,
                    max_keepalive_connections=8,
                    # The Prefect Cloud LB will keep connections alive for 30s.
                    # Only allow the client to keep connections alive for 25s.
                    keepalive_expiry=25,
                ),
            )

            # See https://www.python-httpx.org/http2/
            # Enabling HTTP/2 support on the client does not necessarily mean that your requests
            # and responses will be transported over HTTP/2, since both the client and the server
            # need to support HTTP/2. If you connect to a server that only supports HTTP/1.1 the
            # client will use a standard HTTP/1.1 connection instead.
            httpx_settings.setdefault("http2", PREFECT_API_ENABLE_HTTP2.value())

            if server_type:
                self.server_type = server_type
            else:
                self.server_type = (
                    ServerType.CLOUD
                    if api.startswith(PREFECT_CLOUD_API_URL.value())
                    else ServerType.SERVER
                )

        # Connect to an in-process application
        else:
            self._ephemeral_app = api
            self.server_type = ServerType.EPHEMERAL

        # See https://www.python-httpx.org/advanced/#timeout-configuration
        httpx_settings.setdefault(
            "timeout",
            httpx.Timeout(
                connect=PREFECT_API_REQUEST_TIMEOUT.value(),
                read=PREFECT_API_REQUEST_TIMEOUT.value(),
                write=PREFECT_API_REQUEST_TIMEOUT.value(),
                pool=PREFECT_API_REQUEST_TIMEOUT.value(),
            ),
        )

        if not PREFECT_TESTING_UNIT_TEST_MODE:
            httpx_settings.setdefault("follow_redirects", True)

        enable_csrf_support = (
            self.server_type != ServerType.CLOUD
            and PREFECT_CLIENT_CSRF_SUPPORT_ENABLED.value()
        )

        self._client = PrefectHttpxSyncClient(
            **httpx_settings, enable_csrf_support=enable_csrf_support
        )

        # See https://www.python-httpx.org/advanced/#custom-transports
        #
        # If we're using an HTTP/S client (not the ephemeral client), adjust the
        # transport to add retries _after_ it is instantiated. If we alter the transport
        # before instantiation, the transport will not be aware of proxies unless we
        # reproduce all of the logic to make it so.
        #
        # Only alter the transport to set our default of 3 retries, don't modify any
        # transport a user may have provided via httpx_settings.
        #
        # Making liberal use of getattr and isinstance checks here to avoid any
        # surprises if the internals of httpx or httpcore change on us
        if isinstance(api, str) and not httpx_settings.get("transport"):
            transport_for_url = getattr(self._client, "_transport_for_url", None)
            if callable(transport_for_url):
                server_transport = transport_for_url(httpx.URL(api))
                if isinstance(server_transport, httpx.HTTPTransport):
                    pool = getattr(server_transport, "_pool", None)
                    if isinstance(pool, httpcore.ConnectionPool):
                        setattr(pool, "_retries", 3)

        self.logger: Logger = get_logger("client")

    @property
    def api_url(self) -> httpx.URL:
        """
        Get the base URL for the API.
        """
        return self._client.base_url

    # Context management ----------------------------------------------------------------

    def __enter__(self) -> "SyncPrefectClient":
        """
        Start the client.

        If the client is already started, this will raise an exception.

        If the client is already closed, this will raise an exception. Use a new client
        instance instead.
        """
        if self._closed:
            # httpx.Client does not allow reuse so we will not either.
            raise RuntimeError(
                "The client cannot be started again after closing. "
                "Retrieve a new client with `get_client()` instead."
            )

        self._context_stack += 1

        if self._started:
            # allow reentrancy
            return self

        self._client.__enter__()
        self._started = True

        return self

    def __exit__(self, *exc_info: Any) -> None:
        """
        Shutdown the client.
        """
        self._context_stack -= 1
        if self._context_stack > 0:
            return
        self._closed = True
        self._client.__exit__(*exc_info)

    # API methods ----------------------------------------------------------------------

    def api_healthcheck(self) -> Optional[Exception]:
        """
        Attempts to connect to the API and returns the encountered exception if not
        successful.

        If successful, returns `None`.
        """
        try:
            self._client.get("/health")
            return None
        except Exception as exc:
            return exc

    def hello(self) -> httpx.Response:
        """
        Send a GET request to /hello for testing purposes.
        """
        return self._client.get("/hello")

    def api_version(self) -> str:
        res = self._client.get("/admin/version")
        return res.json()

    def client_version(self) -> str:
        return prefect.__version__

    def raise_for_api_version_mismatch(self) -> None:
        # Cloud is always compatible as a server
        if self.server_type == ServerType.CLOUD:
            return

        try:
            api_version = self.api_version()
        except Exception as e:
            if "Unauthorized" in str(e):
                raise e
            raise RuntimeError(f"Failed to reach API at {self.api_url}") from e

        api_version = version.parse(api_version)
        client_version = version.parse(self.client_version())

        if api_version.major != client_version.major:
            raise RuntimeError(
                f"Found incompatible versions: client: {client_version}, server: {api_version}. "
                f"Major versions must match."
            )

    def create_flow(self, flow: "FlowObject[Any, Any]") -> UUID:
        """
        Create a flow in the Prefect API.

        Args:
            flow: a [Flow][prefect.flows.Flow] object

        Raises:
            httpx.RequestError: if a flow was not created for any reason

        Returns:
            the ID of the flow in the backend
        """
        return self.create_flow_from_name(flow.name)

    def create_flow_from_name(self, flow_name: str) -> UUID:
        """
        Create a flow in the Prefect API.

        Args:
            flow_name: the name of the new flow

        Raises:
            httpx.RequestError: if a flow was not created for any reason

        Returns:
            the ID of the flow in the backend
        """
        flow_data = FlowCreate(name=flow_name)
        response = self._client.post("/flows/", json=flow_data.model_dump(mode="json"))

        flow_id = response.json().get("id")
        if not flow_id:
            raise httpx.RequestError(f"Malformed response: {response}")

        # Return the id of the created flow
        return UUID(flow_id)

    def create_flow_run(
        self,
        flow: "FlowObject[Any, R]",
        name: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        context: Optional[dict[str, Any]] = None,
        tags: Optional[Iterable[str]] = None,
        parent_task_run_id: Optional[UUID] = None,
        state: Optional["prefect.states.State[R]"] = None,
    ) -> FlowRun:
        """
        Create a flow run for a flow.

        Args:
            flow: The flow model to create the flow run for
            name: An optional name for the flow run
            parameters: Parameter overrides for this flow run.
            context: Optional run context data
            tags: a list of tags to apply to this flow run
            parent_task_run_id: if a subflow run is being created, the placeholder task
                run identifier in the parent flow
            state: The initial state for the run. If not provided, defaults to
                `Scheduled` for now. Should always be a `Scheduled` type.

        Raises:
            httpx.RequestError: if the Prefect API does not successfully create a run for any reason

        Returns:
            The flow run model
        """
        parameters = parameters or {}
        context = context or {}

        if state is None:
            state = prefect.states.Pending()

        # Retrieve the flow id
        flow_id = self.create_flow(flow)

        flow_run_create = FlowRunCreate(
            flow_id=flow_id,
            flow_version=flow.version,
            name=name,
            parameters=parameters,
            context=context,
            tags=list(tags or []),
            parent_task_run_id=parent_task_run_id,
            state=state.to_state_create(),
            empirical_policy=FlowRunPolicy(
                retries=flow.retries,
                retry_delay=int(flow.retry_delay_seconds or 0),
            ),
        )

        flow_run_create_json = flow_run_create.model_dump(mode="json")
        response = self._client.post("/flow_runs/", json=flow_run_create_json)
        flow_run = FlowRun.model_validate(response.json())

        # Restore the parameters to the local objects to retain expectations about
        # Python objects
        flow_run.parameters = parameters

        return flow_run

    def update_flow_run(
        self,
        flow_run_id: UUID,
        flow_version: Optional[str] = None,
        parameters: Optional[dict[str, Any]] = None,
        name: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        empirical_policy: Optional[FlowRunPolicy] = None,
        infrastructure_pid: Optional[str] = None,
        job_variables: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        """
        Update a flow run's details.

        Args:
            flow_run_id: The identifier for the flow run to update.
            flow_version: A new version string for the flow run.
            parameters: A dictionary of parameter values for the flow run. This will not
                be merged with any existing parameters.
            name: A new name for the flow run.
            empirical_policy: A new flow run orchestration policy. This will not be
                merged with any existing policy.
            tags: An iterable of new tags for the flow run. These will not be merged with
                any existing tags.
            infrastructure_pid: The id of flow run as returned by an
                infrastructure block.

        Returns:
            an `httpx.Response` object from the PATCH request
        """
        params: dict[str, Any] = {}
        if flow_version is not None:
            params["flow_version"] = flow_version
        if parameters is not None:
            params["parameters"] = parameters
        if name is not None:
            params["name"] = name
        if tags is not None:
            params["tags"] = tags
        if empirical_policy is not None:
            params["empirical_policy"] = empirical_policy.model_dump(
                mode="json", exclude_unset=True
            )
        if infrastructure_pid:
            params["infrastructure_pid"] = infrastructure_pid
        if job_variables is not None:
            params["job_variables"] = job_variables

        flow_run_data = FlowRunUpdate(**params)

        return self._client.patch(
            f"/flow_runs/{flow_run_id}",
            json=flow_run_data.model_dump(mode="json", exclude_unset=True),
        )

    def read_flow_run(self, flow_run_id: UUID) -> FlowRun:
        """
        Query the Prefect API for a flow run by id.

        Args:
            flow_run_id: the flow run ID of interest

        Returns:
            a Flow Run model representation of the flow run
        """
        try:
            response = self._client.get(f"/flow_runs/{flow_run_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return FlowRun.model_validate(response.json())

    def read_flow_runs(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        work_pool_filter: Optional[WorkPoolFilter] = None,
        work_queue_filter: Optional[WorkQueueFilter] = None,
        sort: Optional[FlowRunSort] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[FlowRun]:
        """
        Query the Prefect API for flow runs. Only flow runs matching all criteria will
        be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            work_pool_filter: filter criteria for work pools
            work_queue_filter: filter criteria for work pool queues
            sort: sort criteria for the flow runs
            limit: limit for the flow run query
            offset: offset for the flow run query

        Returns:
            a list of Flow Run model representations
                of the flow runs
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "work_pools": (
                work_pool_filter.model_dump(mode="json") if work_pool_filter else None
            ),
            "work_pool_queues": (
                work_queue_filter.model_dump(mode="json") if work_queue_filter else None
            ),
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }

        response = self._client.post("/flow_runs/filter", json=body)
        return pydantic.TypeAdapter(list[FlowRun]).validate_python(response.json())

    def set_flow_run_state(
        self,
        flow_run_id: UUID,
        state: "prefect.states.State[T]",
        force: bool = False,
    ) -> OrchestrationResult[T]:
        """
        Set the state of a flow run.

        Args:
            flow_run_id: the id of the flow run
            state: the state to set
            force: if True, disregard orchestration logic when setting the state,
                forcing the Prefect API to accept the state

        Returns:
            an OrchestrationResult model representation of state orchestration output
        """
        state_create = state.to_state_create()
        state_create.state_details.flow_run_id = flow_run_id
        state_create.state_details.transition_id = uuid4()
        try:
            response = self._client.post(
                f"/flow_runs/{flow_run_id}/set_state",
                json=dict(
                    state=state_create.model_dump(mode="json", serialize_as_any=True),
                    force=force,
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

        result: OrchestrationResult[T] = OrchestrationResult.model_validate(
            response.json()
        )
        return result

    def set_flow_run_name(self, flow_run_id: UUID, name: str) -> httpx.Response:
        flow_run_data = FlowRunUpdate(name=name)
        return self._client.patch(
            f"/flow_runs/{flow_run_id}",
            json=flow_run_data.model_dump(mode="json", exclude_unset=True),
        )

    def set_task_run_name(self, task_run_id: UUID, name: str) -> httpx.Response:
        task_run_data = TaskRunUpdate(name=name)
        return self._client.patch(
            f"/task_runs/{task_run_id}",
            json=task_run_data.model_dump(mode="json", exclude_unset=True),
        )

    def create_task_run(
        self,
        task: "TaskObject[P, R]",
        flow_run_id: Optional[UUID],
        dynamic_key: str,
        id: Optional[UUID] = None,
        name: Optional[str] = None,
        extra_tags: Optional[Iterable[str]] = None,
        state: Optional[prefect.states.State[R]] = None,
        task_inputs: Optional[
            dict[
                str,
                list[
                    Union[
                        TaskRunResult,
                        Parameter,
                        Constant,
                    ]
                ],
            ]
        ] = None,
    ) -> TaskRun:
        """
        Create a task run

        Args:
            task: The Task to run
            flow_run_id: The flow run id with which to associate the task run
            dynamic_key: A key unique to this particular run of a Task within the flow
            id: An optional ID for the task run. If not provided, one will be generated
                server-side.
            name: An optional name for the task run
            extra_tags: an optional list of extra tags to apply to the task run in
                addition to `task.tags`
            state: The initial state for the run. If not provided, defaults to
                `Pending` for now. Should always be a `Scheduled` type.
            task_inputs: the set of inputs passed to the task

        Returns:
            The created task run.
        """
        tags = set(task.tags).union(extra_tags or [])

        if state is None:
            state = prefect.states.Pending()

        retry_delay = task.retry_delay_seconds
        if isinstance(retry_delay, list):
            retry_delay = [int(rd) for rd in retry_delay]
        elif isinstance(retry_delay, float):
            retry_delay = int(retry_delay)

        task_run_data = TaskRunCreate(
            id=id,
            name=name,
            flow_run_id=flow_run_id,
            task_key=task.task_key,
            dynamic_key=dynamic_key,
            tags=list(tags),
            task_version=task.version,
            empirical_policy=TaskRunPolicy(
                retries=task.retries,
                retry_delay=retry_delay,
                retry_jitter_factor=task.retry_jitter_factor,
            ),
            state=state.to_state_create(),
            task_inputs=task_inputs or {},
        )

        content = task_run_data.model_dump_json(exclude={"id"} if id is None else None)

        response = self._client.post("/task_runs/", content=content)
        return TaskRun.model_validate(response.json())

    def read_task_run(self, task_run_id: UUID) -> TaskRun:
        """
        Query the Prefect API for a task run by id.

        Args:
            task_run_id: the task run ID of interest

        Returns:
            a Task Run model representation of the task run
        """
        try:
            response = self._client.get(f"/task_runs/{task_run_id}")
            return TaskRun.model_validate(response.json())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

    def read_task_runs(
        self,
        *,
        flow_filter: Optional[FlowFilter] = None,
        flow_run_filter: Optional[FlowRunFilter] = None,
        task_run_filter: Optional[TaskRunFilter] = None,
        deployment_filter: Optional[DeploymentFilter] = None,
        sort: Optional[TaskRunSort] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[TaskRun]:
        """
        Query the Prefect API for task runs. Only task runs matching all criteria will
        be returned.

        Args:
            flow_filter: filter criteria for flows
            flow_run_filter: filter criteria for flow runs
            task_run_filter: filter criteria for task runs
            deployment_filter: filter criteria for deployments
            sort: sort criteria for the task runs
            limit: a limit for the task run query
            offset: an offset for the task run query

        Returns:
            a list of Task Run model representations
                of the task runs
        """
        body: dict[str, Any] = {
            "flows": flow_filter.model_dump(mode="json") if flow_filter else None,
            "flow_runs": (
                flow_run_filter.model_dump(mode="json", exclude_unset=True)
                if flow_run_filter
                else None
            ),
            "task_runs": (
                task_run_filter.model_dump(mode="json") if task_run_filter else None
            ),
            "deployments": (
                deployment_filter.model_dump(mode="json") if deployment_filter else None
            ),
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }
        response = self._client.post("/task_runs/filter", json=body)
        return pydantic.TypeAdapter(list[TaskRun]).validate_python(response.json())

    def set_task_run_state(
        self,
        task_run_id: UUID,
        state: prefect.states.State[Any],
        force: bool = False,
    ) -> OrchestrationResult[Any]:
        """
        Set the state of a task run.

        Args:
            task_run_id: the id of the task run
            state: the state to set
            force: if True, disregard orchestration logic when setting the state,
                forcing the Prefect API to accept the state

        Returns:
            an OrchestrationResult model representation of state orchestration output
        """
        state_create = state.to_state_create()
        state_create.state_details.task_run_id = task_run_id
        response = self._client.post(
            f"/task_runs/{task_run_id}/set_state",
            json=dict(state=state_create.model_dump(mode="json"), force=force),
        )
        result: OrchestrationResult[Any] = OrchestrationResult.model_validate(
            response.json()
        )
        return result

    def read_task_run_states(self, task_run_id: UUID) -> list[prefect.states.State]:
        """
        Query for the states of a task run

        Args:
            task_run_id: the id of the task run

        Returns:
            a list of State model representations of the task run states
        """
        response = self._client.get(
            "/task_run_states/", params=dict(task_run_id=str(task_run_id))
        )
        return pydantic.TypeAdapter(list[prefect.states.State]).validate_python(
            response.json()
        )

    def read_deployment(
        self,
        deployment_id: UUID,
    ) -> DeploymentResponse:
        """
        Query the Prefect API for a deployment by id.

        Args:
            deployment_id: the deployment ID of interest

        Returns:
            a [Deployment model][prefect.client.schemas.objects.Deployment] representation of the deployment
        """
        try:
            response = self._client.get(f"/deployments/{deployment_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return DeploymentResponse.model_validate(response.json())

    def read_deployment_by_name(
        self,
        name: str,
    ) -> DeploymentResponse:
        """
        Query the Prefect API for a deployment by name.

        Args:
            name: A deployed flow's name: <FLOW_NAME>/<DEPLOYMENT_NAME>

        Raises:
            prefect.exceptions.ObjectNotFound: If request returns 404
            httpx.RequestError: If request fails

        Returns:
            a Deployment model representation of the deployment
        """
        try:
            response = self._client.get(f"/deployments/name/{name}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise

        return DeploymentResponse.model_validate(response.json())

    def update_flow_run_labels(
        self, flow_run_id: UUID, labels: KeyValueLabelsField
    ) -> None:
        """
        Updates the labels of a flow run.
        """
        response = self._client.patch(
            f"/flow_runs/{flow_run_id}/labels",
            json=labels,
        )
        response.raise_for_status()

    def read_block_document_by_name(
        self,
        name: str,
        block_type_slug: str,
        include_secrets: bool = True,
    ) -> BlockDocument:
        """
        Read the block document with the specified name that corresponds to a
        specific block type name.

        Args:
            name: The block document name.
            block_type_slug: The block type slug.
            include_secrets (bool): whether to include secret values
                on the Block, corresponding to Pydantic's `SecretStr` and
                `SecretBytes` fields. These fields are automatically obfuscated
                by Pydantic, but users can additionally choose not to receive
                their values from the API. Note that any business logic on the
                Block may not work if this is `False`.

        Raises:
            httpx.RequestError: if the block document was not found for any reason

        Returns:
            A block document or None.
        """
        try:
            response = self._client.get(
                f"/block_types/slug/{block_type_slug}/block_documents/name/{name}",
                params=dict(include_secrets=include_secrets),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == status.HTTP_404_NOT_FOUND:
                raise prefect.exceptions.ObjectNotFound(http_exc=e) from e
            else:
                raise
        return BlockDocument.model_validate(response.json())