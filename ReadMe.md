# Aiko Chat: Distributed group discussion application

## Overview

Demonstration of using Aiko Services to implement a group discussion frontend and backend server.

The Aiko Chat frontend CLI can be used to start, interact with and stop the backend server.

![](aiko_chat_diagram.png)

## Set-up development environment

```bash
python3 -m venv venv
source venv/bin/activate

git clone https://github.com/geekscape/aiko_services.git
cd aiko_services
pip install -e .
cd ..

git clone https://github.com/geekscape/aiko_chat.git
cd aiko_chat
pip install -e .
```

## Usage
An MQTT server, e.g `mosquitto` and the `Aiko Registrar` need to be already running ... see [this script](https://github.com/geekscape/aiko_services/blob/master/scripts/system_start.sh).

Each of the following terminal sessions needs to be operating in the development environment's **Python virtual environment**.

### Start Aiko Dashboard for monitoring and diagnosis

```bash
# Terminal session 1
$ aiko_dashboard
```

### Start the Aiko Chat Service
```bash
# Terminal session 2
$ cd src/aiko_chat
$ ./chat.py run  # Blocks until the Aiko Chat Service is terminated
```

### Send a message, then terminate the Aiko Chat Service
When the message is sent, the resulting output should appear on Terminal session 2 (above)

```bash
# Terminal session 3
$ cd src/aiko_chat
$ ./chat.py send r0,r1 message  # Sends a "message" to recipients "r0,r1,r2"

$ ./chat.py exit  # Terminates the AIko Chat Service process
```