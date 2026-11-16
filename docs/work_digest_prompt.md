# Work digest prompt

Give this to the assistant inside your work environment (for example Microsoft 365 Copilot). Paste what it writes into Tether's Settings, under Work digest, or paste it to Claude, which passes it to `ingest_digest` unchanged.

Run it once or twice a day. Re-running is safe: an email already in Tether is counted as a duplicate, and one you archived stays archived.

---

Review my Outlook inbox for emails received in the last 24 hours that need me to do something: reply, decide, approve, sign, review, attend or send something. Skip newsletters, notifications, FYI-only mail and anything I have already replied to.

For each one, write a block in exactly this format, with each label on its own line:

ITEM
From: <sender's name>
Subject: <subject line exactly as written>
Received: <date and time received, as YYYY-MM-DD HH:MM in my local time>
Needed: <one sentence: what I need to do>
Due: <YYYY-MM-DD if the email states or clearly implies a deadline, otherwise none>
Priority: <high, normal or low>
Link: <the Outlook web link to the email, or none>

Use high only when there is a deadline within two business days or the sender is waiting on me to proceed. Do not add any other text, headings or commentary. If nothing needs action, write only: NO ACTION ITEMS
