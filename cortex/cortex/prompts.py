"""prompts.py — single source for Cortex's tunable constants.

Why one file (Zack 2026-05-31): so the things you actually tune — model picks,
the regex/keyword patterns that drive deterministic routing, and the LLM system
prompts — live in ONE place to review and edit, instead of being scattered
across server.py / classifier.py / router.py / session_router.py /
shortcut_config.py / session_browser.py / whisper_pipeline.py.

What's HERE (predefined values, 「预定义的量」):
  - MODELS    — which model each LLM step uses (+ env overrides)
  - PATTERNS  — every regex / keyword set used to MATCH or TRIGGER behaviour
  - PROMPTS   — the system prompts handed to the LLMs

What's NOT here (logic, not constants):
  - the runtime BRIEF builders — agent_brief.py (the Claude-agent brief) and
    distiller.py (the Twin distiller brief). They interleave content with logic
    (f-strings, conditionals) and already each live in their own dedicated file.
  - the per-call user-prompt assemblers (router._build_user_prompt etc.).
  - markdown_runs._INLINE_PATTERNS — that's render formatting, not routing.

Consumers import from here and keep their local names via `as` aliases, so every
usage site is unchanged — only the definitions moved.
"""

from __future__ import annotations

import os
import re


# ════════════════════════════════════════════════════════════════════ MODELS
# Every LLM step defaults to gpt-5.2 with an env override chain. Cortex's OpenAI
# key is configured for it; set the env vars to point a step elsewhere.

# Intent classifier (simple vs complex). Falls back to the router model.
CLASSIFIER_MODEL = os.environ.get(
    "CORTEX_CLASSIFIER_MODEL",
    os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2"),
)

# Two-pass planner (selector + planner) on the simple path.
ROUTER_MODEL = "gpt-5.2"

# Voice-addressable session router (which session does this turn belong to).
SESSION_ROUTER_MODEL = os.environ.get(
    "CORTEX_SESSION_ROUTER_MODEL",
    os.environ.get("CORTEX_CLASSIFIER_MODEL",
                   os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2")),
)

# Voice shortcut-slot config parser.
SHORTCUT_PARSER_MODEL = os.environ.get(
    "CORTEX_CLASSIFIER_MODEL",
    os.environ.get("CORTEX_ROUTER_MODEL", "gpt-5.2"),
)


# ══════════════════════════════════════════════════════════════════ PATTERNS

# ── Vision cues (the cue 「视觉」 + a 「细节视觉」 detail upgrade) ────────────────
# Zack 2026-06-01: vision is a DETERMINISTIC keyword opt-in — saying 「视觉」 (or
# "vision") captures a frame and hands it UNCHANGED to whichever path runs. The
# cue ALONE → 'standard' tier (1024px / q85 — fast, low-bandwidth scene glance).
# Prefixing it → 「细节视觉」 → 'detail' tier (2048px / q90 — the legible-text sweet
# spot for poster / sign / document, per the Claude+GPT vision docs). 「细节」 is a
# common word, so it NEVER triggers a capture on its own — only the 「视觉」 cue
# does; 「细节」 merely UPGRADES the tier when 「视觉」 also fired. Both patterns carry
# STT mishears + pinyin + English so a slightly-misheard cue still works; the
# STT-review card is the backstop. Cortex sends only the tier NAME in the
# request_image frame; the glasses map name → (px, quality). No image→text tool,
# no broad heuristic — 「视觉」 is the ONLY capture trigger.
VISION_KEYWORD_PATTERN = re.compile(
    # [视視]/[觉覺] char classes so the cue fires whether Whisper (lang=auto) emits
    # Simplified 视觉 OR Traditional 視覺 (a real 2026-06-01 miss: it output 視覺).
    r"([视視]\s?[觉覺]|[视視]\s?角|[视視]\s?决|[试試]\s?[觉覺]|事觉|世觉|实觉|是觉|vision|visual|sh[ií]?\s?ju[eé])",
    re.IGNORECASE,
)
# Detail-tier upgrade marker — consulted ONLY after the 「视觉」 cue already
# captured (so a bare 「细节」 in ordinary speech can't fire a photo). Matches the
# 「细节」 of 「细节视觉」 + its mishears, plus 高清 / 2k / English "detail".
VISION_DETAIL_PATTERN = re.compile(
    # [细細][节節] covers Simplified 细节 + Traditional 細節 (Whisper lang=auto may
    # emit either); rest are STT mishears of 细节 + 高清 / 2k / English "detail".
    r"([细細][节節]|细接|细结|系节|喜节|西街|习节|xi\s?jie|detail|高清|高分辨率|高解析|2k)",
    re.IGNORECASE,
)

# ── Model override (deterministic model pin) ───────────────────────────────
# Zack 2026-06-01: naming a model in the ask PINS its path — no LLM guess, same
# spirit as the vision cue. "gpt"/"chatgpt"/"openai" → the simple router path
# (GPT answers / acts directly); "claude"/"克劳德"/"cloud" → the complex agent
# path. 'cloud' is the most common STT mishear of "Claude" (accepted trade-off:
# "cloud storage" etc. may over-trigger the agent, which degrades gracefully).
# Claude wins ties — the agent path can fall back to a single action, but the
# router can't escalate to research.
#
# NOTE: substring match, NOT \b-anchored. CJK chars are \w in Python regex, so a
# \b never forms between 用 and "gpt" in 「用gpt推理」 → \bgpt\b would never fire on
# Chinese-embedded model names (real 2026-06-01 miss). The [pb] also catches the
# common STT mishear "gbt"; \s? tolerates spelled-out "g p t" / "g b t".
GPT_OVERRIDE_PATTERN = re.compile(r"(g\s?[pb]\s?t|open\s?ai)", re.IGNORECASE)
CLAUDE_OVERRIDE_PATTERN = re.compile(r"(claude|克劳德|克劳|cloud)", re.IGNORECASE)

# ── Permission mode (full-auto opt-in) ─────────────────────────────────────
# Full-auto (bypassPermissions) is opt-in ONLY via the explicit phrase "自动模式"
# (Zack 2026-05-30: "有'自动模式'这四个字的时候，才能开"). No other phrasing enables it —
# everything else stays acceptEdits, so permission requests surface as cards.
AUTO_RUN_PATTERNS = [re.compile(r"自动模式")]

# ── Pin / unpin a session (whole-utterance) ────────────────────────────────
# R-14.b / C-56: anchored so the test is "is the ENTIRE prompt a pin command?"
# — not "does it contain pin words" (which would false-positive on "pin a
# reminder about X").
PIN_INTENT_PATTERNS = [
    # English
    re.compile(r"^\s*(please\s+)?(pin|keep)( this| it| this one| this session)?\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*keep( this)? alive\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*don'?t (kill|drop|evict) this\.?\s*$", re.IGNORECASE),
    # Chinese
    re.compile(r"^\s*(请\s*)?(钉住|留着|保留|别(关|掐|杀)|不要(关|掐|杀))\s*(这个|此|它|这条|这个会话|这个对话|这个 session)?\s*[。.!！]?\s*$"),
]
UNPIN_INTENT_PATTERNS = [
    re.compile(r"^\s*(please\s+)?unpin( this| it| this one| this session)?\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*release( this)?\.?\s*$", re.IGNORECASE),
    # Chinese
    re.compile(r"^\s*(取消(钉住|保留)|不(钉|留)了|解除(钉住|保留))\s*(这个|此|它)?\s*[。.!！]?\s*$"),
]

# ── Decision vocabulary (ring-emitted card tokens) ──────────────────────────
# The 3-option contract every blocking card carries; and the canonical tokens
# the RING emits for each terminal decision. The ring maps every gesture to a
# fixed token before it reaches Cortex (TAP→"Approve", LONG→"modify",
# double-tap→"Kill" — see Glass StateMachine), so these are the ONLY decision
# strings that ever arrive. The old free-text synonym lists (send / lgtm / 没问题
# / cancel / 算了 …) + spoken-reply content sniffing died with the ring-only
# interaction model — no spoken phrase ever becomes the `decision` field, so any
# token beyond these three was dead. (Zack 2026-06-01: prune the dead paths.)
THREE_OPTIONS = ["Approve", "Modify", "Kill"]
APPROVE_BUTTON_TOKENS = {"approve"}
MODIFY_BUTTON_TOKENS = {"modify"}
KILL_BUTTON_TOKENS = {"kill"}

# ── UC2 session browse / pick ──────────────────────────────────────────────
# "show me my sessions in <project>" (EN + ZH). Kept narrow so it doesn't shadow
# ordinary asks; the project name is extracted separately.
BROWSE_PATTERNS = [
    re.compile(
        r"\b(list|show|what|which|recent)\b.*\b(session|sessions|conversation|conversations|chat|chats|threads?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(列(一?下)?|看(一?下)?|有哪些|最近).*(session|会话|对话|聊天|线程)"),
]
# Stopwords stripped when guessing the project name out of a browse utterance.
BROWSE_STOPWORDS = {
    "list", "show", "me", "my", "the", "recent", "last", "few", "what", "which",
    "are", "is", "in", "of", "for", "project", "projects", "folder", "session",
    "sessions", "conversation", "conversations", "chat", "chats", "thread",
    "threads", "under", "from", "please", "go", "into",
}
# Pick a listed session by number / ordinal.
PICK_NUM = re.compile(r"#\s*(\d+)|\bnumber\s+(\d+)|\bsession\s+(\d+)\b|第\s*([0-9一二两三四五六])\s*[个条]?", re.IGNORECASE)
PICK_ORDINAL = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
}
ZH_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}
# Strip connective words when pick-by-TOPIC ("the gesture one" / "接着那个 auth 的").
PICK_STRIP = re.compile(
    r"\b(continue|go|in|into|open|resume|the|one|that|this|session|sessions|conversation|please)\b",
    re.IGNORECASE,
)
PICK_STRIP_ZH = re.compile(r"(接着|继续|到|进|那个|这个|里(边|面)?|跑|去|会话|对话|的)")

# ── Shortcut-slot config detection ─────────────────────────────────────────
# An utterance names a slot (shortcut N / 快捷方式 N) AND has a config verb. A
# fire ("run shortcut 2") has no config verb, so it falls through to normal.
SLOT_RE = re.compile(r"(?:shortcut|快捷(?:方式|键)?)\s*([1-3])", re.IGNORECASE)
CONFIG_VERB_RE = re.compile(
    r"(set|change|configure|update|edit|rename|make|定义|设为|设成|设置|"
    r"改成|改为|换成|配置|绑定|编辑)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════ PROMPTS

# ── Intent classifier (simple vs complex) ──────────────────────────────────
CLASSIFIER_SYSTEM = """\
You're Cortex's intent classifier. Single job: route Zack's ask to either the
research-agent path (Claude — multi-step) or the direct-adapter path (one
bounded side-effect or state read).

Output JSON ONLY: {"complex": true|false, "why": "≤15 words"}

complex = true  WHEN the ask needs ANY of:
  - reading/finding/searching multiple sources (emails, files, sessions, web)
  - composition / drafting / summarising
  - multiple side-effect actions in one ask
  - keywords: find / look at / search / summarise / draft / check / 看 / 找 / 起草

complex = false WHEN it's a SINGLE explicit step:
  - bounded reminder: "remind me to X at 3pm" (title + time both given)
  - bounded calendar: "add a 4pm meeting with Y tomorrow"
  - bounded message: "send 'on my way' to Mike" (recipient + content both given)
  - pure state query: "battery?", "what time?", "focus mode?", "current tab?"
  - bounded file write: "write 'X' to /tmp/y.txt"

A photo may be attached ("Note: photo attached."). It does NOT change this
decision — classify on the TASK, not the photo: a bounded one-step ask is simple
even with a photo ("add a reminder for the time in this photo", "what is this");
a multi-step / compose / persist-to-twin ask is complex.

When ambiguous, prefer complex=true — the agent path can degrade to a single
action; the direct path can't escalate to research.

JSON ONLY. No fence. No prose.
"""


# ── Router planner (pass 2: emit the dispatch plan) ────────────────────────
ROUTER_PLANNER_SYSTEM = """\
You are Cortex Router, the brain of Zack's personal AI system "Constellation". Each
turn you read a brief and emit a dispatch plan as JSON. Cortex executes it with
local Mac tools and renders results on Zack's AR glasses.

RULES
1. Never side-effect without preview. Set requires_confirm=true on mutating
   subtasks, or leave it null for Cortex's confirm-policies to decide.
2. Use only tools in YOUR TOOLS. If a capability is missing, set
   primary_intent="unsupported" and name what's missing in `reasoning`.
3. When Zack names a person, look up people/core/<slug>.md in the Twin and pull
   `email:` / `phone:` from frontmatter. Never invent a contact.
4. Fewest subtasks that do the job.
5. ISO 8601 for date/time args; resolve "tomorrow" / "3pm" against NOW.
6. Photo attached? You can see it — read it to answer ("what is this", "what
   does it say") or to fill an adapter's args (e.g. the time on a poster). A pure
   visual answer → put it in hud_show body_template (subtasks:[]); an ask that
   ACTS on the image → plan the adapter subtask with the values you read off it.

result_format
  query   — bounded state read (battery, current tab, today's events), no side effect
  execute — side effect (add reminder, send email, run shortcut, …)
  (`draft` is legacy — composition/research now lives in the agent path, not here)

SCOPE — you handle SINGLE-SHOT plans only. Any ask needing multi-step
research, drafting, or mid-task user judgment was routed to the agent path
upstream and never reaches you. So: ONE round, ONE plan, then either
hud_show (info) or preview_action (one batch of subtasks pending confirm).

OUTPUT — think briefly in plain text if useful, then emit JSON inside a ```json
fence (Cortex parses only the fence):

{
  "primary_intent": "kebab-case",
  "subtasks": [
    {
      "tool":   "<from YOUR TOOLS>",
      "action": "<documented action>",
      "args":   { ... ISO dates ... },
      "context_pack": [],
      "result_format": "execute" | "query",
      "requires_confirm": true | false | null
    }
  ],
  "hud_response": {
    "kind":  "preview_action" | "hud_show",
    "icon":  "one of ✉ ⌖ ⚙ ✦ ✓",
    "title": "short, specific",
    "body_template": "markdown; supports {{subtasks[i].result.field}} interpolation",
    "options": ["1–4 button labels"]
  },
  // NOTE: never emit kind="tool_card". That kind is reserved for the reverse-wake
  // path which Cortex builds directly without invoking you. For user-initiated
  // side-effecting actions use preview_action; for pure info use hud_show.
  "reasoning": "one sentence"
}

FEEDBACK RE-PLAN — when ZACK'S WORDS appears on a follow-up call, treat it as
a correction to the PRIOR plan. Apply the change (e.g. "4pm not 10am" updates
the value; "skip" emits hud_show + empty subtasks + brief ack). Emit ONE
revised plan; you won't be re-invoked again on this ask.

HUD BODY — every card is INFO + a yield point, not a yes/no question. body_template
must carry enough for Zack to judge / correct / skip without tapping.
  Bad:  "Send the reply to Jane?"
  Good: "Reply ready:\\n\\nHey Jane —\\n\\nI'll be there at 3.\\n\\n— Zack"
  Bad:  "Add reminder?"
  Good: "Reminder: meeting with 云, 5/29 14:00 (from 'CHI draft sync')"
"""


# ── Router selector (pass 1: pick which Twin slices the planner needs) ──────
ROUTER_SELECTOR_SYSTEM = """\
You are Cortex's Twin selector. Zack just spoke; pick which Twin files the
planner needs to plan the next action — and ONLY those.

Output JSON, nothing else: {"paths": ["identity.md", "skills/X.md", ...]}

Rules:
- Include identity.md ONLY if the ask is about Zack himself, his preferences,
  his style, or naming/addressing him. Mechanical asks (status, time, "open
  X") don't need it.
- Include people/core/<slug>.md ONLY when Zack names that person (by name or
  one of their aliases listed in the TOC).
- Include skills/X.md ONLY when X is directly relevant to the immediate ask.
  Don't pre-emptively grab adjacent skills "just in case".
- Maximum 5 paths. Median good answer is 1–3. ALL paths must be from the TOC.
- When unsure between two skills, pick the more specific one.
- Empty list is valid. Don't pad. Empty is better than wrong.

JSON only. No prose. No markdown fences.
"""


# ── Session router (which active session does this turn belong to) ─────────
SESSION_ROUTER_SYSTEM = """\
You're Cortex's session router. Two jobs:

(1) ROUTING — given a new user prompt and a list of currently-active CC sessions,
    decide which session the prompt belongs to OR whether to create a new one.

(2) CROSS-SESSION CONTEXT (R-14.d) — if the prompt asks to USE INFORMATION FROM
    other session(s) while continuing/starting THIS one, list those OTHER
    session_ids in `context_from`. Example: a user in session B saying "draft
    an email using the auth-refactor session's action items" → continue B and
    set context_from=[<auth-refactor session_id>]. NEVER include the target
    session in context_from. NEVER use context_from to "do work in" another
    session — context_from only LENDS context to the chosen target.

Output JSON ONLY: {"decision": "continue"|"new", "target_session_id": "ses_..."|null, "confidence": 0.0-1.0, "why": "≤15 words", "context_from": ["ses_...", ...]}

Routing rules:
- If the prompt clearly references an existing session's topic (subject overlap,
  pronouns like "the email one"/"that auth refactor"/"那个邮件"/"那个 auth"),
  pick decision="continue" + that session_id. High confidence (0.8-1.0).
- If the prompt is a clearly new topic unrelated to all active sessions,
  pick decision="new" + target_session_id=null. High confidence.
- If the prompt is a short follow-up ("yes", "more", "next", "嗯", "继续") AND
  there's exactly one recently-active session (last activity < 5 min), continue
  that one with high confidence.
- If ambiguous (prompt could plausibly belong to multiple existing sessions or
  could equally be new), pick the most-recently-active matching session with
  medium confidence (0.5-0.7). The orchestrator may show a confirmation card.
- When in doubt, prefer "continue" the current session (the caller passes
  current_session_id in the input; weight it heavily).
- Default `context_from`: empty list `[]`. Only populate when the prompt
  explicitly cross-references another session.

JSON ONLY. No fence. No prose.
"""


# ── Shortcut-slot config parser ────────────────────────────────────────────
SHORTCUT_PARSER_SYSTEM = """\
You configure one of the user's 3 voice shortcut slots. The user's utterance
edits a slot — extract what it should become. The slot, when later fired,
sends a PRESET PROMPT to the assistant (optionally with a photo attached).

Output JSON ONLY:
{"slot": 1|2|3, "prompt": "<the preset prompt to send when fired>", "send_photo": true|false, "label": "<≤4-word name>"}

Rules:
- `slot`: the slot number the user named (1, 2, or 3).
- `prompt`: the actual prompt the shortcut will SEND when fired — NOT the
  configuration command. E.g. for "set shortcut 2 to ask what's in front of
  me", prompt = "What's in front of me?" (not "set shortcut 2 …").
- `send_photo`: true if firing should attach a camera photo — set true when the
  user says to send/attach a photo, OR when the prompt inherently needs to see
  the scene (what's in front, read this, identify this). Else false.
- `label`: a short human name for the slot (≤4 words), derived from the prompt.
- Preserve the user's language for `prompt` + `label` (Chinese in → Chinese out).

JSON ONLY. No fence. No prose.
"""


# ════════════════════════════════════════════════════════════════════ WHISPER
# Forced when lang="zh": nudges whisper-cli to emit Simplified (not Traditional).
WHISPER_ZH_PROMPT = "请用简体中文输出"
