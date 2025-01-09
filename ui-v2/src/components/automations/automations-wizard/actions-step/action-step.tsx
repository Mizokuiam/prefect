import { Button } from "@/components/ui/button";
import { FormMessage } from "@/components/ui/form";
import { Icon } from "@/components/ui/icons";
import { Typography } from "@/components/ui/typography";
import { useEffect } from "react";
import { useFormContext } from "react-hook-form";
import { ActionTypeAdditionalFields } from "./action-type-additional-fields";
import { type ActionsSchema, UNASSIGNED } from "./action-type-schemas";
import { ActionTypeSelect } from "./action-type-select";

type ActionStepProps = {
	index: number;
	onRemove: () => void;
};

export const ActionStep = ({ index, onRemove }: ActionStepProps) => {
	const form = useFormContext<ActionsSchema>();

	const type = form.watch(`actions.${index}.type`);
	// reset form values based on selected action type
	useEffect(() => {
		switch (type) {
			case "run-deployment":
			case "pause-deployment":
			case "resume-deployment":
				form.setValue(`actions.${index}.deployment_id`, UNASSIGNED);
				break;
			case "pause-work-queue":
			case "resume-work-queue":
				form.setValue(`actions.${index}.work_queue_id`, UNASSIGNED);
				break;
			case "pause-work-pool":
			case "resume-work-pool":
				form.setValue(`actions.${index}.work_pool_id`, UNASSIGNED);
				break;
			case "pause-automation":
			case "resume-automation":
				form.setValue(`actions.${index}.automation_id`, UNASSIGNED);
				break;
			case "send-notification":
			case "cancel-flow-run":
			case "suspend-flow-run":
			case "resume-flow-run":
			case "change-flow-run-state":
			default:
				break;
		}
	}, [form, index, type]);

	return (
		<div key={index} className="flex flex-col gap-2">
			<div className="flex justify-between items-center">
				<Typography variant="body" className="font-semibold">
					Action {index + 1}
				</Typography>
				<Button
					size="icon"
					aria-label={`remove action ${index + 1}`}
					onClick={onRemove}
					variant="outline"
				>
					<Icon id="Trash2" className="h-4 w-4" />
				</Button>
			</div>
			<ActionTypeSelect index={index} />
			<ActionTypeAdditionalFields index={index} actionType={type} />
			<hr className="my-10" />
		</div>
	);
};
