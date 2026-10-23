# EspDrop host app (M4)

AirDrop on Windows and Linux through an ESP32-C5/C3 that runs its own IPv6
stack on the AWDL link. Design: [`docs/M4_ENTWURF.md`](../docs/M4_ENTWURF.md);
how to test each stage: [`docs/SPIKES.md`](../docs/SPIKES.md).

| module | role |
|---|---|
| `espdrop/link.py` | USB framing and message formats (shared with `tools/espdrop_link.py`) |
| `espdrop/transport.py` | serial port: pyserial, or a raw POSIX tty without it |
| `espdrop/device.py` | HELLO, commands, UDP sockets on the device |
| `espdrop/mdns.py` | AirDrop browser over the device's UDP |
| `espdrop/cli.py` | `find`, `status`, `cmd` |
| `tests/` | simulated device with iPad-shaped mDNS answers |

Run from a checkout with the OpenDrop venv's python (it has python-zeroconf):

```sh
~/opendrop-venv/bin/python tools/espdrop.py find
~/opendrop-venv/bin/python -m unittest discover -s host/tests
```
