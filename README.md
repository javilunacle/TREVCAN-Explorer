TREVCAN-Explorer
================

Simple CAN explorer with a FastAPI backend and React frontend.

It can be used as a general purpose replacement for PCAN-Explorer in addition
to a TREV specific menus and pages. 

Quick start
-----------
1. Install Python deps:
	```
	pip install -r requirements.txt
	```
2. Install frontend deps:
	```
	cd webserver/frontend
	npm install
	cd ../..
	```
3. Start everything:
	```
	python start.py
	```

Run on boot with systemd (Linux)
---------------------------------
The repository includes a systemd installer that creates a service for the
current checkout and enables it at boot:

- `trevcan-explorer.service` starts the TREVCAN backend/frontend stack.

CAN interface setup is not managed by this service. Configure and bring up CAN
interfaces separately before connecting from the app.

1. Install Python dependencies. A repo-local virtual environment is recommended
   on Raspberry Pi OS and will be used automatically by the service installer:
	```
	python3 -m venv .venv
	. .venv/bin/activate
	pip install -r requirements.txt
	```
2. Install the service:
	```
	bash systemd/install_service.sh
	```
3. Check status:
	```
	sudo systemctl status trevcan-explorer.service
	```
4. View logs:
	```
	sudo journalctl -u trevcan-explorer.service -f
	```

If you need a different Python interpreter, run the installer with
`PYTHON_BIN=/path/to/python bash systemd/install_service.sh`.

The service runs the selected Python interpreter with `start.py --service`,
which keeps the backend and frontend running without trying to open a browser
during boot.

On Linux, CAN interfaces already brought up by the system will appear to the app
as SocketCAN devices. Use the SocketCAN/CANable path in the UI and select the
interface you want to use.

Open:
- Frontend: http://localhost:3001
- Backend API: http://localhost:8000

Supported CAN hardware / servers
--------------------------------
- PCAN-USB
- CANable
- Network CAN server (`host:port`)
- Bluetooth CAN server (Windows)

Reliable raw-CAN transport prototype
------------------------------------
For the separate Pi-to-laptop SQLite spool, sequenced TCP receiver, off-car DBC
decoding, and InfluxDB/Grafana simulation walkthrough, see
[`telemetry/README.md`](telemetry/README.md). This does not replace the React
explorer or change the existing (lossy) `StreamForwarder` path.

