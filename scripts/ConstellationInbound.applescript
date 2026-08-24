-- Constellation: inbound email → cortex HUD push (Mail "Run AppleScript" rule).
-- Source of truth; compile to ~/Library/Application Scripts/com.apple.mail/ via:
--   osacompile -o ~/Library/Application\ Scripts/com.apple.mail/ConstellationInbound.scpt \
--              ~/Code/Projects/Constellation-Server/scripts/ConstellationInbound.applescript
--
-- STORM-PROOF (learned the hard way 2026-06-06): a Mail rule fires per delivery,
-- and "apply to existing" / a bulk account sync hands the WHOLE inbox to one
-- firing. Doing `content of m` + spawning a shell per message over hundreds of
-- messages FROZE Mail. Guards below make that impossible:
--   1. kMaxBatch — bail instantly on a big batch (= a bulk op, not new mail).
--   2. kMaxAgeSeconds — only act on genuinely-recent mail (skip old/bulk silently,
--      with only a CHEAP date check — no body read, no shell).
--   3. kMaxPerRun — hard cap on shell spawns per firing.
-- The notifier itself is launched DETACHED so Mail never waits on the network.
property kScript : "~/Code/Projects/Constellation-Server/scripts/mail_inbound_notify.py"
property kMaxBatch : 12          -- >this many messages in one firing = bulk op → bail
property kMaxAgeSeconds : 900    -- only act on mail received in the last 15 min
property kMaxPerRun : 5          -- hard cap on notifications per firing

using terms from application "Mail"
	on perform mail action with messages theMessages for rule theRule
		-- Guard 1: bulk / "apply to existing" hands us the whole inbox → bail with
		-- ZERO work (no body reads, no shells). Real new-mail deliveries are tiny.
		if (count of theMessages) > kMaxBatch then return
		set nowDate to (current date)
		set handled to 0
		tell application "Mail"
			repeat with m in theMessages
				if handled > (kMaxPerRun - 1) then exit repeat
				try
					-- Guard 2: CHEAP recency check first. Old mail (bulk sync /
					-- existing inbox) is skipped without reading the body or
					-- spawning anything.
					set d to date received of m
					if (nowDate - d) > kMaxAgeSeconds then
						-- too old → skip silently
					else
						set msgId to (message id of m) as string
						set theSubject to (subject of m) as string
						set theSender to (sender of m) as string
						set theBody to (content of m) as string
						set acctName to ""
						try
							set acctName to (name of (account of (mailbox of m))) as string
						end try
						my notifyCortex(msgId, theSender, theSubject, acctName, theBody)
						set handled to handled + 1
					end if
				end try
			end repeat
		end tell
	end perform mail action with messages
end using terms from

on notifyCortex(msgId, theSender, theSubject, acctName, theBody)
	set tmpPath to (do shell script "mktemp /tmp/constellation_mail.XXXXXX")
	set fh to open for access (POSIX file tmpPath) with write permission
	try
		set eof of fh to 0
		write theBody to fh as «class utf8»
	end try
	close access fh
	-- DETACHED (trailing &, all fds redirected) so Mail NEVER waits on the network.
	do shell script "/usr/bin/python3 " & quoted form of kScript & " " & ¬
		quoted form of msgId & " " & quoted form of theSender & " " & ¬
		quoted form of theSubject & " " & quoted form of acctName & " " & ¬
		quoted form of tmpPath & " </dev/null >/dev/null 2>&1 &"
end notifyCortex
