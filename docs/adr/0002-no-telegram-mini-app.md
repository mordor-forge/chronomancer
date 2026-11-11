---
status: accepted
---

# No Telegram Mini App

Version 0.1 shipped a Telegram Mini App as the phone surface: a loopback listener, a signed-data check and a second page that repeated the Desktop page. We decided to remove it. It doubled the surface to build and test, it needed the owner to publish an HTTPS address, and it was never run on a real phone. The Hermes mobile app is the planned phone surface. The Desktop page is the only owner surface until then.
