"""applescript_mail adapter — macOS Mail.app via osascript.

Actions: read_current, list_inbox, draft, send, find_messages, get_thread.

See TOOL-ADAPTERS.md §2. Side-effects: read=none, draft=low, send=medium (irreversible).

Compose vs reply vs search — three distinct intents (per Zack 2026-05-24):

  1. REPLY to a specific message — sender account is determined BY the original message.
     - send(reply_to_current=True, body=...)  → reply to currently selected message in Mail UI.
     - send(reply_to_message_id="<id>", body=...) → find message by RFC Message-ID and reply.
     Mail.app's `reply` action auto-routes through the receiving account; do NOT pass
     `account` — it'd be ignored / conflict.

  2. COMPOSE a new message — sender account is user-specified.
     - send(to=[...], subject=..., body=..., account="QQ")  → send from QQ account.
     - draft(...) same shape.
     `account` is matched against Mail.app's account names (iCloud / Google / QQ / UIUC).
     Adapter looks up the account's first email address and sets it as `sender`.

  3. SEARCH historical messages — no send action; for "回忆我跟 X 的邮件" intents.
     - find_messages(participant=..., subject_contains=..., body_contains=..., account=?,
                     mailbox=?, limit=10) → list of message metadata across inbox+sent.

Notification handling: Cortex does NOT manage mail push notifications. Zack's phone +
Glass handle that. The adapter is request/response only.

Safety: `send` action is preview-always per confirm-policies.md. `dry_run=true` on send
routes to draft as a safety pattern.
"""

from __future__ import annotations

import asyncio
from typing import Any


FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


def _esc(s: Any) -> str:
    # Coerce first: a plan/LLM occasionally emits a non-string field (e.g. a
    # numeric body) and a bare .replace() then crashes with
    # "'int' object has no attribute 'replace'", failing the whole send.
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


async def _run_osascript(script: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8"), err.decode("utf-8")


async def _resolve_account_address(account_name: str) -> str:
    """Look up an account's primary email address by display name (case-insensitive)."""
    script = f'''
    tell application "Mail"
        set fs to (ASCII character 31)
        repeat with acc in every account
            if (name of acc as string) is "{_esc(account_name)}" then
                set addrs to email addresses of acc
                if (count of addrs) > 0 then return item 1 of addrs
            end if
        end repeat
        return ""
    end tell
    '''
    rc, stdout, stderr = await _run_osascript(script)
    if rc != 0:
        raise RuntimeError(f"could not resolve account '{account_name}': {stderr.strip()}")
    addr = stdout.strip()
    if not addr:
        raise ValueError(f"applescript_mail: account '{account_name}' not found in Mail.app")
    return addr


class MailAdapter:
    name = "applescript_mail"

    SIDE_EFFECT_ACTIONS = {"send"}

    async def dispatch(
        self,
        action: str,
        args: dict[str, Any],
        context_pack: list[str],
        result_format: str,
    ) -> dict[str, Any]:
        if action in self.SIDE_EFFECT_ACTIONS and result_format == "draft":
            raise ValueError(
                f"applescript_mail.{action} has no draft semantics. Use result_format='execute'."
            )

        if action == "read_current":
            return await self._read_current(args)
        if action == "list_inbox":
            return await self._list_inbox(args)
        if action == "find_messages":
            return await self._find_messages(args)
        if action == "draft":
            return await self._draft(args)
        if action == "send":
            return await self._send(args)
        if action == "get_thread":
            return await self._get_thread(args)
        raise ValueError(f"applescript_mail: unknown action '{action}'")

    # ── reads ──

    async def _read_current(self, args: dict[str, Any]) -> dict[str, Any]:
        script = f'''
        tell application "Mail"
            set fs to (ASCII character 31)
            set sel to selection
            if (count of sel) = 0 then return ""
            set m to item 1 of sel
            return (message id of m) & fs & (subject of m) & fs & (sender of m) & fs & ((date received of m) as string) & fs & (content of m)
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        if not stdout.strip():
            return {"found": False, "message": "No message currently selected in Mail.app."}
        parts = stdout.split(FIELD_SEP)
        return {
            "found": True,
            "message_id": parts[0] if len(parts) > 0 else "",
            "subject": parts[1] if len(parts) > 1 else "",
            "sender": parts[2] if len(parts) > 2 else "",
            "date": parts[3] if len(parts) > 3 else "",
            "body_text": parts[4] if len(parts) > 4 else "",
        }

    async def _list_inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        unread_only = bool(args.get("unread_only", False))
        unread_clause = "whose read status is false" if unread_only else ""
        script = f'''
        tell application "Mail"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            set msgs to (messages of inbox {unread_clause})
            set out to ""
            set i to 0
            repeat with m in msgs
                if i ≥ {limit} then exit repeat
                set out to out & (message id of m) & fs & (subject of m) & fs & (sender of m) & fs & ((date received of m) as string) & fs & (read status of m as string) & rs
                set i to i + 1
            end repeat
            return out
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = []
        for rec in stdout.strip().split(RECORD_SEP):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split(FIELD_SEP)
            if len(parts) < 5:
                continue
            items.append({
                "message_id": parts[0],
                "subject": parts[1],
                "sender": parts[2],
                "date": parts[3],
                "read": parts[4].lower() == "true",
            })
        return {"unread_only": unread_only, "count": len(items), "items": items}

    async def _find_messages(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search across inbox + sent mailboxes for matching messages.

        Filters (all optional, AND-combined):
          participant     — substring matched against sender + recipients (catches both directions)
          subject_contains
          body_contains   — slow; skipped unless explicitly set
          account         — restrict to one account ('iCloud', 'Google', 'QQ', 'UIUC')
          mailbox         — 'inbox' | 'sent' | 'both' (default 'both')
          limit           — default 20
        """
        participant = args.get("participant") or ""
        subject_q = args.get("subject_contains") or ""
        body_q = args.get("body_contains") or ""
        account_filter = args.get("account") or ""
        mailbox_filter = (args.get("mailbox") or "both").lower()
        if mailbox_filter in ("all", "any", "either"):
            mailbox_filter = "both"
        limit = int(args.get("limit", 20))

        if mailbox_filter not in ("inbox", "sent", "both"):
            mailbox_filter = "both"

        sent_mailbox_map = {
            "iCloud": "Sent Messages",
            "Google": "Sent Mail",
            "QQ": "Sent Messages",
            "UIUC": "Sent Items",
        }

        # Build `whose` filter clause to push filtering into Mail.app (orders of magnitude
        # faster than looping over every message in AppleScript userland).
        whose_parts = []
        if subject_q:
            whose_parts.append(f'subject contains "{_esc(subject_q)}"')
        if participant:
            # `whose` can match sender easily; recipient list is nested + slower. We rely on
            # the sent-mailbox scopes to catch outbound messages to `participant`.
            whose_parts.append(f'sender contains "{_esc(participant)}"')
        whose_clause = " and ".join(whose_parts)

        def scope_expr(noun: str) -> str:
            return f"({noun} whose {whose_clause})" if whose_clause else f"({noun})"

        scopes_script = []
        if mailbox_filter in ("inbox", "both"):
            if account_filter:
                scopes_script.append(
                    f'try\nset end of allMsgs to {scope_expr("messages of inbox of account " + chr(34) + _esc(account_filter) + chr(34))}\nend try'
                )
            else:
                scopes_script.append(f'try\nset end of allMsgs to {scope_expr("messages of inbox")}\nend try')
        if mailbox_filter in ("sent", "both"):
            if account_filter:
                sent_box = sent_mailbox_map.get(account_filter, "Sent Messages")
                scopes_script.append(
                    f'try\nset end of allMsgs to {scope_expr("messages of mailbox " + chr(34) + _esc(sent_box) + chr(34) + " of account " + chr(34) + _esc(account_filter) + chr(34))}\nend try'
                )
            else:
                for acc_name, sent_box in sent_mailbox_map.items():
                    scopes_script.append(
                        f'try\nset end of allMsgs to {scope_expr("messages of mailbox " + chr(34) + _esc(sent_box) + chr(34) + " of account " + chr(34) + _esc(acc_name) + chr(34))}\nend try'
                    )
        scope_block = "\n".join(scopes_script)

        script = f'''
        tell application "Mail"
            set fs to (ASCII character 31)
            set rs to (ASCII character 30)
            set allMsgs to {{}}
            {scope_block}
            set out to ""
            set i to 0
            repeat with msgList in allMsgs
                repeat with m in msgList
                    if i ≥ {limit} then exit repeat
                    set keep to true
                    if "{_esc(body_q)}" is not "" then
                        try
                            if (content of m) does not contain "{_esc(body_q)}" then set keep to false
                        end try
                    end if
                    if keep then
                        set out to out & (message id of m) & fs & (subject of m) & fs & (sender of m) & fs & ((date received of m) as string) & rs
                        set i to i + 1
                    end if
                end repeat
                if i ≥ {limit} then exit repeat
            end repeat
            return out
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        items = []
        for rec in stdout.strip().split(RECORD_SEP):
            rec = rec.strip()
            if not rec:
                continue
            parts = rec.split(FIELD_SEP)
            if len(parts) < 4:
                continue
            items.append({
                "message_id": parts[0],
                "subject": parts[1],
                "sender": parts[2],
                "date": parts[3],
            })
        return {
            "filters": {
                "participant": participant or None,
                "subject_contains": subject_q or None,
                "body_contains": body_q or None,
                "account": account_filter or None,
                "mailbox": mailbox_filter,
            },
            "count": len(items),
            "items": items,
        }

    async def _get_thread(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"thread_id": args.get("message_id"), "note": "single-message stub; v1 limitation"}

    # ── writes ──

    async def _draft(self, args: dict[str, Any]) -> dict[str, Any]:
        to_list = args.get("to") or []
        if isinstance(to_list, str):
            to_list = [to_list]
        if not to_list:
            raise ValueError("applescript_mail.draft: 'to' (string or list) required")
        subject = args.get("subject") or ""
        body = args.get("body") or ""
        account = args.get("account")

        sender_clause = ""
        if account:
            addr = await _resolve_account_address(account)
            sender_clause = f', sender:"{_esc(addr)}"'

        recipients_script = "\n".join(
            f'make new to recipient with properties {{address:"{_esc(addr)}"}}'
            for addr in to_list
        )
        script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{_esc(subject)}", content:"{_esc(body)}", visible:false{sender_clause}}}
            tell newMsg
                {recipients_script}
            end tell
            save newMsg
            return id of newMsg as string
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {
            "draft_id": stdout.strip(),
            "to": to_list,
            "subject": subject,
            "account": account,
            "saved_to_drafts": True,
        }

    async def _send(self, args: dict[str, Any]) -> dict[str, Any]:
        # Safety: dry_run → route to draft.
        if args.get("dry_run"):
            result = await self._draft(args)
            result["dry_run"] = True
            return result

        body = args.get("body") or ""

        # Variant A: reply to currently selected message
        if args.get("reply_to_current"):
            script = f'''
            tell application "Mail"
                set sel to selection
                if (count of sel) = 0 then error "No message selected to reply to."
                set m to item 1 of sel
                set replyMsg to reply m with opening window
                tell replyMsg
                    set content to "{_esc(body)}"
                end tell
                send replyMsg
                return id of replyMsg as string
            end tell
            '''
            rc, stdout, stderr = await _run_osascript(script)
            if rc != 0:
                raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
            return {"message_id": stdout.strip(), "variant": "reply_to_current"}

        # Variant D: reply by message_id (Mail finds the message and reuses its account)
        if args.get("reply_to_message_id"):
            target_id = args["reply_to_message_id"]
            # Search inbox + sent across all accounts for this message id.
            script = f'''
            tell application "Mail"
                set found to missing value
                try
                    set found to (first message of inbox whose message id is "{_esc(target_id)}")
                end try
                if found is missing value then
                    repeat with acc in every account
                        try
                            set boxes to mailboxes of acc
                            repeat with b in boxes
                                try
                                    set found to (first message of b whose message id is "{_esc(target_id)}")
                                    exit repeat
                                end try
                            end repeat
                            if found is not missing value then exit repeat
                        end try
                    end repeat
                end if
                if found is missing value then error "no message with that message_id"
                set replyMsg to reply found with opening window
                tell replyMsg
                    set content to "{_esc(body)}"
                end tell
                send replyMsg
                return id of replyMsg as string
            end tell
            '''
            rc, stdout, stderr = await _run_osascript(script)
            if rc != 0:
                raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
            return {
                "message_id": stdout.strip(),
                "variant": "reply_to_message_id",
                "in_reply_to": target_id,
            }

        # Variant C: compose + send (with optional account)
        to_list = args.get("to") or []
        if isinstance(to_list, str):
            to_list = [to_list]
        if not to_list:
            raise ValueError(
                "applescript_mail.send: need one of 'to' / 'reply_to_current' / 'reply_to_message_id'"
            )
        subject = args.get("subject") or ""
        account = args.get("account")

        sender_clause = ""
        sender_addr = None
        if account:
            sender_addr = await _resolve_account_address(account)
            sender_clause = f', sender:"{_esc(sender_addr)}"'

        recipients_script = "\n".join(
            f'make new to recipient with properties {{address:"{_esc(addr)}"}}'
            for addr in to_list
        )
        script = f'''
        tell application "Mail"
            set newMsg to make new outgoing message with properties {{subject:"{_esc(subject)}", content:"{_esc(body)}", visible:false{sender_clause}}}
            tell newMsg
                {recipients_script}
            end tell
            send newMsg
            return id of newMsg as string
        end tell
        '''
        rc, stdout, stderr = await _run_osascript(script)
        if rc != 0:
            raise RuntimeError(f"osascript failed (rc={rc}): {stderr.strip()}")
        return {
            "message_id": stdout.strip(),
            "to": to_list,
            "subject": subject,
            "account": account,
            "sender": sender_addr,
            "variant": "compose_send",
        }
