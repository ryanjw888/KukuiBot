# Report Delivery

## Rule (non-negotiable)

When delivering a report or artifact to the user (via email, chat, or file link), you MUST verify the artifact exists, is non-empty, and passes sanitization before delivery. Never send a broken link, empty file, or unsanitized content.

## When This Fires

- Another worker has completed a report/artifact and you are delivering it
- The user asks you to email a report that was previously generated
- You are relaying results from a delegated task that produced a file

## The Delivery Protocol

1. **Verify the artifact exists** — Read the file path. Confirm it is not empty (>100 bytes for HTML, >50 bytes for text).
2. **Sanitize check** — Scan for sensitive content per the Email Data Sanitization Policy:
   - No MAC addresses (unless masked)
   - No API keys, tokens, or credentials
   - No local filesystem paths in the body
   - No account IDs or personal contact details (beyond the recipient)
3. **Compose the delivery message** — Clear subject, brief summary of what the report contains, and how the user can access it.
4. **Send via appropriate channel:**
   - **Email:** Use the Gmail send-report API with the absolute file path
   - **Chat:** Provide the file path or URL where the user can view it
   - **Both:** If the user hasn't specified, offer both options
5. **Confirm delivery** — Verify the send succeeded (check API response for email, confirm file accessibility for links).

## Delivery Message Template

```
Subject: [Report Type] — [Client/Context] — [Date]

Summary: [1-2 sentences describing what the report covers]
Key findings: [2-3 bullet points of highlights, if applicable]

[Link or attachment info]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The file was just generated, it's obviously there." | Obviously there is not verified. Read it. |
| "Sanitization is overkill for internal reports." | Internal reports get forwarded. Sanitize everything. |
| "I'll just send the path, the user can open it." | Confirm the path is accessible first. Broken links erode trust. |
| "The generating worker already sanitized it." | Trust but verify. Check it yourself. |

## Red Flags (self-check)

- You are about to email a file you haven't verified exists
- You are sending a file path without confirming it's accessible
- You haven't checked for sensitive content in the report body
- Your delivery message has no subject or summary
- You are sending without confirming the Gmail permission level allows the recipient

## Hard Gate

Delivery is BLOCKED until:
1. Artifact existence is verified (file read, non-empty)
2. Sensitive content scan passes
3. Gmail permissions checked (if sending via email)
4. Delivery confirmation evidence included in response
