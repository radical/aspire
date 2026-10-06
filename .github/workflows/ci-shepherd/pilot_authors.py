"""Human instruction authority is distinct from identified Copilot reports."""

APPROVED_USERS = {1472: "radical"}
REVIEWER = "copilot-pull-request-reviewer[bot]"
REVIEWER_ID = 175728472
WORKER_ID = 198982749


def approved(user):
    return (isinstance(user, dict) and type(user.get("id")) is int
            and user["id"] in APPROVED_USERS and user.get("login") == APPROVED_USERS[user["id"]])


def copilot(user):
    # The documented reviewer alias and cloud-agent alias resolve to two
    # distinct stable IDs; REST can report their canonical login as "Copilot".
    # https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-code-review
    aliases = {REVIEWER_ID: {"Copilot", REVIEWER},
               WORKER_ID: {"Copilot", "copilot-swe-agent[bot]"}}
    return (isinstance(user, dict) and type(user.get("id")) is int
            and user["id"] in aliases and isinstance(user.get("login"), str)
            and user["login"] in aliases[user["id"]]
            and user.get("type") == "Bot")


def feedback(user):
    return approved(user) or copilot(user)
