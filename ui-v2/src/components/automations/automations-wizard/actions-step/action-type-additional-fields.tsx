import { ActionChangeFlowRunStateFields } from "./action-change-flow-run-state-fields";
import type { ActionType } from "./action-type-schemas";
import { AutomationsSelectStateFields } from "./automations-select-state-fields";

type ActionTypeAdditionalFieldsProps = {
	actionType: ActionType;
	index: number;
};

export const ActionTypeAdditionalFields = ({
	actionType,
	index,
}: ActionTypeAdditionalFieldsProps) => {
	switch (actionType) {
		case "change-flow-run-state":
			return <ActionChangeFlowRunStateFields index={index} />;
		case "run-deployment":
		case "pause-deployment":
		case "resume-deployment":
			return <div>TODO Deployment</div>;
		case "pause-work-queue":
		case "resume-work-queue":
			return <div>TODO Work Queue</div>;
		case "pause-work-pool":
		case "resume-work-pool":
			return <div>TODO Work pool</div>;
		case "pause-automation":
			return <AutomationsSelectStateFields action="Pause" index={index} />;
		case "resume-automation":
			return <AutomationsSelectStateFields action="Resume" index={index} />;
		case "send-notification":
			return <div>TODO send notification</div>;
		case "cancel-flow-run":
		case "suspend-flow-run":
		case "resume-flow-run":
		default:
			return null;
	}
};
