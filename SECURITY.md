# Security

AirBound puts a Home Assistant on the internet, so reports about its security are welcome and taken seriously.

## Reporting a vulnerability

Please report it privately: open the repository's **Security** tab and choose
[**Report a vulnerability**](https://github.com/oleost/AirBound/security/advisories/new).
Only the maintainer sees the report. Please don't open a public issue for something that could be
used against other people's homes before a fix is out.

It helps to include:

- what an attacker can do, and what they need for it (an account, a link someone must click, access to the relay, ...)
- the AirBound version, and how it is connected: the hosted relay, your own relay server, or directly through the router
- steps to reproduce it

You should get an answer within a week. AirBound is maintained by one person, so a fix can take
longer than that; you will be told what is planned and when the fix is released.

## What is covered

- The add-on in `airbound/`, including the login guard that enforces two-step login.
- The relay server in `relay-server/` and the hosted relay at airbound.dev.
- The build and release tooling in this repository.

Please test against your own Home Assistant and your own relay server. Don't test against other
people's addresses under airbound.dev, and don't load-test the hosted relay.

Home Assistant itself, Caddy, frp and CrowdSec have their own security contacts. If you are not sure
where a fault belongs, report it here and it will be passed on.

## Supported versions

Only the newest release gets fixes. Home Assistant offers the update as soon as it is released.
