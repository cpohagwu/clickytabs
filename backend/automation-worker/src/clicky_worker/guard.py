"""The irreversible-step gate: enforced in code, whatever the agent model does.

The agent is told to call ask_user before irreversible steps, and Claude does - but in testing a
small local model clicked "Place order" without asking. So every click goes through this gate first: if the clicked
element's label reads like an irreversible action (place order, pay, buy, send, delete, book, ...), the user is asked
in the tracker before the click happens, unless they just approved an ask_user question (one approval covers one
such click). The gate also keeps coordinate clicking off (a click must name an element, so its label can be checked)
and the `evaluate` action (JavaScript in the page could click anything) is removed by the runner.

Enter submits too: `send_keys` with Enter (also Control+Enter) and `input` text with a line break (browser-use
types it as an Enter key press) are checked the same way. The gate reads the field that would get the key (the
focused element, through CDP; or the input's target in browser-use's DOM tree): inside a form, it asks when one of the
form's submit buttons reads as irreversible ("Press Enter to submit “Place order” on shop.example?"); outside a form,
it asks when the field is a message / reply / comment box (Enter sends there). If it can't tell, it asks.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from clicky_worker.hitl import Confirmations

IRREVERSIBLE = re.compile(
    r"\b("
    r"place (?:my |your |the )?order|order now|complete (?:order|purchase|booking|payment|checkout)"
    r"|confirm (?:and pay|order|purchase|payment|booking|transfer)|submit (?:order|payment)"
    r"|buy(?: now| it now)?|purchase|pay(?: now)?|proceed to payment|check ?out now"
    r"|send(?: now| message| email| money)?|post|publish|tweet|reply"
    r"|delete|close account|remove account|cancel (?:my |your )?(?:order|subscription|account|booking)"
    r"|unsubscribe|transfer|donate|book(?: now)?|reserve|subscribe|sign up|accept (?:terms|and continue)"
    r")\b",
    re.IGNORECASE,
)
MAX_LABEL = 80
ENTER_KEY = re.compile(r"\b(?:enter|return|numpadenter)\b", re.IGNORECASE)
MESSAGE_FIELD = re.compile(r"\b(?:message|reply|comment|post|tweet|chat|caption|status|write)\b", re.IGNORECASE)
SUBMIT_TYPES = {"submit", "image"}

# What Enter would act on: the focused element (through open shadow roots), its form's submit buttons, its name.
FOCUS_PROBE = r"""(() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el === document.body) return { editable: false, inForm: false, submits: [], field: '' };
  const name = (n) => ((n.getAttribute && (n.getAttribute('aria-label') || n.getAttribute('title'))) || n.value
    || n.innerText || n.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const form = el.form || (el.closest ? el.closest('form') : null);
  const submits = form ? [...form.querySelectorAll(
    'button:not([type]), button[type=submit], input[type=submit], input[type=image]')].map(name).filter(Boolean) : [];
  const field = ['aria-label', 'placeholder', 'name', 'id'].map((a) => el.getAttribute(a) || '').join(' ').trim();
  const editable = !!el.isContentEditable || ['INPUT', 'TEXTAREA'].includes(el.tagName);
  return { editable, inForm: !!form, submits, field: field.slice(0, 120) };
})()"""


def irreversible(label: str) -> bool:
    return bool(IRREVERSIBLE.search(" ".join(label.split())))


def element_label(node: Any) -> str:
    """What the user would read on the element: its text, or its aria-label / value / title / alt."""
    parts: list[str] = []
    try:
        parts.append(node.get_all_children_text(max_depth=4))
    except Exception:
        pass
    attributes = getattr(node, "attributes", None) or {}
    for key in ("aria-label", "value", "title", "alt"):
        value = attributes.get(key)
        if value:
            parts.append(value)
    return " ".join(" ".join(part.split()) for part in parts if part).strip()[:MAX_LABEL]


def form_submit_labels(node: Any) -> tuple[bool, list[str]]:
    """(inside a form?, labels of that form's submit buttons) from browser-use's DOM tree."""
    form = node
    while form is not None and (getattr(form, "tag_name", "") or "") != "form":
        form = getattr(form, "parent_node", None)
    if form is None:
        return False, []
    labels: list[str] = []
    stack = list(getattr(form, "children_nodes", None) or [])
    while stack:
        current = stack.pop()
        tag = getattr(current, "tag_name", "") or ""
        kind = ((getattr(current, "attributes", None) or {}).get("type") or "").lower()
        if (tag == "button" and kind in ("", "submit")) or (tag == "input" and kind in SUBMIT_TYPES):
            if label := element_label(current):
                labels.append(label)
        stack.extend(getattr(current, "children_nodes", None) or [])
    return True, labels


def enter_question(probe: dict[str, Any] | None) -> tuple[str, str] | None:
    """(label, question with {host}) when Enter here may commit the user to something; None when it can't."""
    if not isinstance(probe, dict):
        return "Enter", "Press Enter on {host}? (I couldn't tell what it would submit.)"
    submits = [str(label) for label in probe.get("submits") or []]
    if probe.get("inForm"):
        risky = next((label for label in submits if irreversible(label)), None)
        return (risky, f"Press Enter to submit “{risky}” on {{host}}?") if risky else None
    if probe.get("editable") and MESSAGE_FIELD.search(str(probe.get("field") or "")):
        return "Enter (send)", "Press Enter to send what I typed on {host}?"
    return None


async def focused_probe(browser_session: Any) -> dict[str, Any] | None:
    """FOCUS_PROBE in the agent's current page (our code, not the model's: `evaluate` stays excluded)."""
    try:
        session = await browser_session.get_or_create_cdp_session(target_id=None, focus=False)
        reply = await session.cdp_client.send.Runtime.evaluate(
            params={"expression": FOCUS_PROBE, "returnByValue": True}, session_id=session.session_id)
        value = (reply or {}).get("result", {}).get("value")
        return value if isinstance(value, dict) else None
    except Exception:
        return None


class Gate:
    def __init__(self, confirmations: Confirmations, task_id: str) -> None:
        self._confirmations = confirmations
        self._task_id = task_id
        self._approved = 0
        self.asked: list[str] = []          # labels the gate asked about (logs, tests)

    def grant_once(self) -> None:
        """The user approved the agent's own ask_user question: the next irreversible click goes ahead."""
        self._approved = 1

    async def check(self, action: Any, browser_session: Any) -> Any | None:
        """None to let the action run, or an ActionResult telling the agent the user said no."""
        from browser_use.agent.views import ActionResult

        for name, params in action.model_dump(exclude_unset=True).items():
            if not isinstance(params, dict):
                continue
            asking = await self._question(name, params, browser_session)
            if asking is None:
                continue
            label, question = asking
            if self._approved:
                self._approved -= 1
                continue
            url = ""
            with _quiet():
                url = await browser_session.get_current_page_url()
            host = urlsplit(url).hostname or "this site"
            self.asked.append(label)
            answer = await self._confirmations.ask(self._task_id, question.format(host=host))
            if not answer.approved:
                reason = f" ({answer.note})" if answer.note else ""
                what = f"clicking “{label}”" if name == "click" else "pressing Enter there"
                return ActionResult(error=f"The user did not approve {what}{reason}. Do not try it again or "
                                          "another way: finish now and report what you did so far.")
        return None

    @staticmethod
    async def _question(name: str, params: dict[str, Any], browser_session: Any) -> tuple[str, str] | None:
        if name == "click" and params.get("index") is not None:
            node = await browser_session.get_element_by_index(int(params["index"]))
            label = element_label(node) if node is not None else ""
            return (label, f"Click “{label}” on {{host}}?") if label and irreversible(label) else None
        if name == "input" and params.get("index") is not None and re.search(r"[\r\n]", str(params.get("text"))):
            node = await browser_session.get_element_by_index(int(params["index"]))
            if node is None:
                return enter_question(None)
            in_form, submits = form_submit_labels(node)
            attributes = getattr(node, "attributes", None) or {}
            field = " ".join(str(attributes.get(key) or "") for key in ("aria-label", "placeholder", "name", "id"))
            return enter_question({"inForm": in_form, "submits": submits, "editable": True, "field": field})
        if name == "send_keys" and ENTER_KEY.search(str(params.get("keys") or "")):
            return enter_question(await focused_probe(browser_session))
        return None


class _quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True                         # a page URL is a nicety for the question, never a failure


def guarded_tools(gate: Gate, exclude_actions: list[str]) -> Any:
    """browser-use Tools whose every action passes the gate first, with coordinate clicking kept off."""
    from browser_use import Tools

    class GuardedTools(Tools):  # type: ignore[misc, valid-type]
        def set_coordinate_clicking(self, enabled: bool) -> None:
            super().set_coordinate_clicking(False)

        async def act(self, action: Any, browser_session: Any, *args: Any, **kwargs: Any) -> Any:
            refused = await gate.check(action, browser_session)
            if refused is not None:
                return refused
            return await super().act(action, browser_session, *args, **kwargs)

    return GuardedTools(exclude_actions=exclude_actions)
