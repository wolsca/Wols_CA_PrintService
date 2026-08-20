# Wols CA Print Service (Home Assistant add-on)

This add-on runs exactly the same service as the Debian/Ubuntu installation: the
same code, the same CUPS intake queues (`WolsCA_Booklet`, `WolsCA_DoubleSided`,
`WolsCA_SingleSided`), the same output queue `WolsCA_Output` and the same
`WolsCAPrintService.json`. Nothing about the configuration differs; the add-on
options are simply written into that JSON before the service starts.

## Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add
   `https://github.com/wolsca/Wols_CA_PrintService`.
2. Install **Wols CA Print Service** (the *(test)* add-on is only for trying out
   commit builds - see below).
3. Fill in the options (at minimum the MQTT broker, user and password, and the
   printer URI) and start the add-on.

## Interface in Home Assistant

Unchanged in the way it works: everything is exposed through MQTT discovery, so
the same set of entities appears as with the Debian installation - status, last
error, resume, cancel, the self-test, the update controls and the administrator
device. They are grouped under their own device, because this instance is
labelled `HA` (see below).

There is deliberately **no ingress panel and no sidebar entry**. The web app
keeps running on its own port (`web_port`, default `8080`), reachable at
`http://<ha-host>:8080/` and through the *Open Web UI* button on the add-on page.

## Options

| Option | Written into the configuration |
|---|---|
| `instance_id` | `mqtt.instance_id`; also prefixes the topic with `<instance_id>_` |
| `mqtt_broker`, `mqtt_port` | `mqtt.broker_ip`, `mqtt.broker_port` |
| `mqtt_user`, `mqtt_password` | `settings.user`, `settings.password` |
| `mqtt_topic_prefix`, `mqtt_discovery_prefix` | `mqtt.topic_prefix`, `mqtt.discovery_prefix` |
| `printer_uri` | `hardware.printer_uri` (the physical printer) |
| `flip_timeout_seconds` | `hardware.flip_timeout_seconds` |
| `print_mode` | `settings.print_mode` (default mode) |
| `web_port`, `web_title`, `web_language` | `web.port`, `web.title`, `web.language` |
| `admin_token` | `web.admin_token`; empty means the administrator card stays locked |
| `auto_update`, `allow_test_builds` | `update.auto_update`, `update.allow_test_builds` |

Leaving `mqtt_broker`, `mqtt_user` or `mqtt_password` empty keeps whatever is in
the configuration file already; the Mosquitto add-on can also be discovered
through the `mqtt` service, so those fields are usually only needed for an
external broker.

Everything the options do not cover (intake queues, printer targets,
notifications, history) stays in `/data/WolsCAPrintService.json`, survives
restarts and updates, and can be edited from the administrator card in the web
app.

## Running next to a Debian installation

Both can use the same broker. The add-on marks itself as its own instance:

- The MQTT topic prefix gets the instance marker in front of it, so
  `wols_ca/printer` becomes `HA_wols_ca/printer`. The prefix is added at
  start-up, is applied only once and never has to be typed in by hand.
- Every entity gets its own `unique_id` (suffix `_ha`), its own discovery node
  and its own device *Wols CA Print Service (HA)*, so Home Assistant shows the
  two side by side instead of overwriting each other.
- A Debian installation keeps `mqtt.instance_id` empty and therefore keeps
  exactly the entities it has today - nothing to migrate.

Only change `instance_id` if you run more than two instances; the value ends up
in the topic and in the entity ids.

## Printing to the add-on

The add-on uses the **host network**, because Windows and Android only find the
printers through mDNS/DNS-SD and CUPS has to answer on port 631. Consequences:

- Port 631 on the host must be free - do not run a second CUPS on the same
  machine.
- The queues are reachable as `ipp://<ha-host>:631/printers/WolsCA_Booklet`
  (`_DoubleSided`, `_SingleSided`).
- `web_port` must not clash with anything else on the host; 8080 is the default,
  change it if it is taken.

## Updates

The add-on version follows the **published releases** only, so the Supervisor
offers an update when a release is cut. To try a commit build, install the
separate *Wols CA Print Service (test)* add-on - its version follows every
commit. Both can be installed at the same time: the test add-on has its own
`instance_id` (`HAtest`) and its own `web_port`.

## Self-test

The service's own diagnostics work here as well: press the self-test button in
Home Assistant or open the self-test card in the web app. The `update` phase
reports that there is no git checkout inside the container, which is expected -
in the add-on the Supervisor does the updating.
