# CANWeb
Web application that can be used to view CAN traffic

## Features

- **Raw traffic view** – scrolling mode (infinite scroll) and refresh-in-place mode (one row per CAN ID)
- **DBC decoding** – upload a `.dbc` file to decode signal values in real time
- **Live plots** – line plots, scatter plots, and histograms per decoded signal, organised into tabs
- **Transmit** – send one-shot or periodic CAN frames
- **Bus statistics** – bus load %, baud rate, error frame count
- **Export** – raw CSV, HTML plot snapshot, JSON/YAML config save & load
- **Hardware support** – PCAN, Kvaser, SocketCAN, Vector, IXXAT, SLCAN, and a built-in simulator for demo use

## Quick start

```bash
pip install -r requirements.txt
python run.py
# Open http://localhost:5000
```

By default the app uses a **simulated bus** that generates random CAN frames so you can explore the UI without any hardware.

## Connecting real hardware

Open the **Settings** tab and choose your interface:

| Interface | python-can driver | Typical channel |
|-----------|-------------------|-----------------|
| PCAN USB  | `pcan`            | `PCAN_USBBUS1`  |
| Kvaser    | `kvaser`          | `0`             |
| SocketCAN | `socketcan`       | `can0`          |
| Vector    | `vector`          | `0`             |
| IXXAT     | `ixxat`           | `0`             |
| SLCAN     | `serial`          | `/dev/ttyUSB0`  |

## DBC decoding

1. Go to the **Decoded Signals** tab
2. Click **Browse** and select a `.dbc` file
3. Signal values appear live in the table and become available in the **Plots** tab

## Plots

1. Go to the **Plots** tab → click **+ Add Plot Tab**
2. Choose a plot type (Line / Scatter / Histogram), select a signal, and click **Add**
3. The chart updates every 500 ms
4. Click **Export HTML** to download a standalone HTML file of all plots

## Config files

Configs are JSON or YAML files that store the current plot layout and interface settings.  
Use **Save Config** / **Load Config** in the Plots toolbar.

## Project layout

```
CANWeb/
├── app/
│   ├── __init__.py        # Flask app, SocketIO handlers, REST API
│   ├── can_manager.py     # python-can wrapper + simulator
│   ├── dbc_manager.py     # cantools DBC loader / decoder
│   ├── static/
│   │   ├── css/style.css
│   │   └── js/app.js
│   └── templates/index.html
├── run.py                 # Entry point
├── requirements.txt
└── tests/
    └── test_api.py
```

