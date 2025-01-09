import { z } from "zod";

export const UNASSIGNED = "UNASSIGNED";

const FlowRunSchema = z.object({
	type: z.enum(["cancel-flow-run", "suspend-flow-run", "resume-flow-run"]),
});

const ChangeFlowRunStateSchema = z.object({
	type: z.literal("change-flow-run-state"),
	state: z.enum([
		"COMPLETED",
		"RUNNING",
		"SCHEDULED",
		"PENDING",
		"FAILED",
		"CANCELLED",
		"CANCELLING",
		"CRASHED",
		"PAUSED",
	]),
	name: z.string().optional(),
	message: z.string().optional(),
});

const RunDeploymentsSchema = z.object({
	type: z.literal("run-deployment"),
	deployment_id: z.string(),
	job_variables: z.record(z.unknown()),
	parameters: z.record(z.unknown()),
});

const DeploymentsSchema = z.object({
	type: z.enum(["pause-deployment", "resume-deployment"]),
	/** nb: Because shadcn MUST have a non empty sting as a value, use UNASSIGNED to indicate that this will turn to a null value */
	deployment_id: z.string().or(z.literal(UNASSIGNED)),
});

const WorkQueueSchema = z.object({
	type: z.enum(["pause-work-queue", "resume-work-queue"]),
	/** nb: Because shadcn MUST have a non empty sting as a value, use UNASSIGNED to indicate that this will turn to a null value */
	work_queue_id: z.string().or(z.literal(UNASSIGNED)),
});

const WorkPoolSchema = z.object({
	type: z.enum(["pause-work-pool", "resume-work-pool"]),
	/** nb: Because shadcn MUST have a non empty sting as a value, use UNASSIGNED to indicate that this will turn to a null value */
	work_pool_id: z.string().or(z.literal(UNASSIGNED)),
});

const AutomationSchema = z.object({
	type: z.enum(["pause-automation", "resume-automation"]),
	/** nb: Because shadcn MUST have a non empty sting as a value, use UNASSIGNED to indicate that this will turn to a null value */
	automation_id: z.string().or(z.literal(UNASSIGNED)),
});

const SendNotificationSchema = z.object({
	type: z.literal("send-notification"),
	block_document_id: z.string(),
	body: z.string(),
	subject: z.string(),
});

export const ActionsSchema = z.object({
	actions: z.array(
		z.union([
			ChangeFlowRunStateSchema,
			DeploymentsSchema,
			RunDeploymentsSchema,
			WorkPoolSchema,
			WorkQueueSchema,
			AutomationSchema,
			SendNotificationSchema,
			FlowRunSchema,
		]),
	),
});

export type ActionsSchema = z.infer<typeof ActionsSchema>;
export type ActionType = ActionsSchema["actions"][number]["type"];
