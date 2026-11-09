# Changelog

## 0.15.5

A security review of AirBound. Nothing here was known to be misused, and there is nothing you have to do.

- **The hosted relay's website can't change how AirBound is set up.** The address and key it hands out are now checked before AirBound uses them, so even if airbound.dev were taken over, it could only give your Home Assistant a plain address under airbound.dev.
- **Linking to the hosted relay is harder to trick.** The login with Patreon or GitHub only starts from the page that shows your code, in the browser that opened it. If your support already covers all its Home Assistants, the link page asks which one the new one replaces; none is unlinked by itself any more.
- The programs that face the internet (the proxy, the relay connection and CrowdSec) no longer get AirBound's own key to Home Assistant.
- With a relay, a connection that claims to come from your home network is refused in a few more cases.

## 0.15.4

- Updated frp, which carries AirBound's connection, to 0.72.0. It fixes a fault where a closed connection could keep a processor core busy, and reconnects faster after a dropped connection.
- Security updates to Caddy, which serves your address.
- **If you run your own relay server:** open the **Relay server** step in AirBound and run its command on the server again to get the new frp there too. Your connection keeps working while you wait.
- With the hosted relay, or through your router, there is nothing you have to do.

## 0.15.3

- Security updates to the parts AirBound is built on: frp, Caddy with its CrowdSec bouncer, CrowdSec and the Python base image.
- **If you run your own relay server, please update it:** open the **Relay server** step in AirBound and run its command on the server again, then restart the server. A fault in the firewall AirBound set up could stop the server's automatic security updates after a restart. This fixes that and installs the updated frp. Your connection keeps working as before.
- With the hosted relay, or through your router, there is nothing you have to do.

## 0.15.1

- This changelog: Home Assistant now shows what is new when AirBound has an update.

## 0.15.0

- **Only apps you approve can log in from outside.** A link to your real login page could name another site as the app you are logging in to, and that site would get your login, two-step code included. AirBound now stops such logins and says why on the login page.
- Your browser and the official Home Assistant apps always work, and devices already logged in stay logged in.
- **If you use your own Alexa or Google Assistant link, or another app that logs in from outside:** the next time it has to log in, allow it under **Allowed apps** on AirBound's overview page. You can also turn the check off there.

## 0.14.6

- GitHub Sponsors is a second way to get access to the hosted relay, besides Patreon.

## 0.14.5

- The airbound.dev website has a tab icon, also on Safari and iPhone home screens.

## 0.14.4

- Documentation brought in line with what AirBound does today.

## 0.14.3

- After a restore from another machine, the notification says how to open AirBound's page.

## 0.14.2

- The key that identifies this machine is never stored in a file a backup holds.
- Linking to airbound.dev again while remote access is on keeps working.

## 0.14.1

- Trying to go live while waiting for your confirmation after a restore now shows a clear message.

## 0.14.0

- **A restored backup can't go live or take over an address by itself.** After a restore, remote access stays off until you confirm in the panel.

Earlier versions are in the [project's history](https://github.com/oleost/AirBound/commits/main).
