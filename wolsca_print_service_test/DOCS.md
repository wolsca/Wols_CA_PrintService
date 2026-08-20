# Wols CA Print Service (test channel)

Same add-on as **Wols CA Print Service**, but its version follows every commit
build instead of the published releases. Use it to try something out; keep the
release add-on for normal operation.

See the documentation of the release add-on for the options and the printing
setup - they are identical.

Two things to keep in mind when both are installed:

- Its `instance_id` is `HAtest`, so the MQTT topic becomes
  `HAtest_wols_ca/printer_test` and every entity, device and discovery node is
  its own. Leave it as it is, otherwise this instance takes over the entities of
  the release add-on.
- It has its own `web_port` (default `8081`), but stop the release add-on while
  testing: both instances would otherwise claim CUPS on port 631 of the host.
