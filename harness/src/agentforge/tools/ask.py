from agentforge.models import ToolResult
from agentforge.permissions import current_answer

NO_ANSWER = ("The human did not provide an answer. Continue with your best judgement "
             "and state the assumption you made.")


class AskUserTool:
    """Ask the human for information the model cannot infer.

    The permission gate suspends the run and waits; the answer arrives on a context
    var rather than as an argument, so a skipped question is never an error. The run
    continues either way -- this tool collects information, it does not gate an action.
    """
    name = "ask_user"
    description = ("Ask the human for information you cannot infer: a missing value, an "
                   "ambiguous requirement, or a choice between options. Use it when a "
                   "guess would likely be wrong and the task cannot continue safely "
                   "without the answer. Do not use it for approval, for credentials, or "
                   "to ask something you can find out with another tool.")
    permission = "confirm"
    source = "builtin"
    schema = {"type": "object",
              "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2000},
                             "options": {"type": "array", "maxItems": 8,
                                         "items": {"type": "string", "maxLength": 200}}},
              "required": ["question"], "additionalProperties": False}

    async def execute(self, args: dict) -> ToolResult:
        answer = current_answer.get()
        if not answer or not answer.strip():
            return ToolResult(ok=True, output=NO_ANSWER)
        return ToolResult(ok=True, output=answer.strip())
