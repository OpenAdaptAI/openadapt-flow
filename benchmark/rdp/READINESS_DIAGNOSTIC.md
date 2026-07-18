# Real-RDP readiness diagnosis

This is a derived, non-counted diagnostic record for the rejected candidate
`a1b152640e36ccd8722e2df33252403078a87653`. Raw screenshots and guest command
output remain local under `/private/tmp`; they are not product evidence and
were not committed because they expose the qualification VM's desktop.

The first input-free diagnostic reproduced the 90-second pre-input halt. A real
Aardwolf session painted Windows' "Another user is signed in" interstitial,
while `query user` showed only the preserved base's console user active and
Explorer existed only in that console session. The temporary RDP connection
never advanced beyond a connected, username-less session. Diagnostic JSON
SHA-256: `a2491d72adfbdf1d64dfe0420841893fcb8dffbf8a7207e19b04159a1013d4f5`.

A fresh owned-snapshot diagnostic then logged off the pre-existing console
session before connection, without sending any RDP input. The temporary account
became the one exact active RDP session and Explorer's independently queried
`SessionId` matched it. A 60-second frame sequence proved the remaining race:

| Elapsed | Observed state | Frame SHA-256 |
| ---: | --- | --- |
| 0 s | Windows Welcome/login transition | `d2bb78c8a6a82ade6a5b477cd5d270663080774ac19aea983264ffb3cd570a39` |
| 10 s | Painted Windows desktop | `c718b644e986d8609a8dbcda327f0dae3cea0e3ac3dca70d115bbe98d40c1fdb` |
| 30 s | Painted desktop; partial update observed | `819a9b5c62f48b20bfbc8e75d5d6ddf972d0192be946198787166769feb59bbf` |
| 60 s | Painted Windows desktop | `00e4ae703f796d542e19e964262c1a30c5f28fc6d798a7e29a9b0e429c7f9c19` |

The final diagnostic JSON SHA-256 is
`8e53c7c088eaa912991b03248397bbccdf91e9f5be00d2b3d14644e8243ae766`.
It records zero RDP input and successful cleanup: exact suspended base current,
owned snapshot deleted, and all seven pre-existing snapshot IDs restored.

The successor candidate therefore refuses input until it proves all of:

1. Pre-existing console/RDP users were logged off inside the harness-owned
   snapshot.
2. The exact qualification account has one active RDP session.
3. Explorer exists in that exact session ID.
4. The framebuffer materially transitions from the login frame and multiple
   stable frames match this fixed VM's Windows desktop/taskbar predicate.
5. Aardwolf framebuffer copies occur on its decoder event-loop thread and any
   embedded `(value, error)` input receipt raises instead of looking successful.

This diagnosis does not count as a qualification trial and makes no Citrix
ICA/HDX claim.
