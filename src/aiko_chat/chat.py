#!/usr/bin/env python3
#
# Usage
# ~~~~~
# ./chat.py run
# ./chat.py exit
#
# ./chat.py repl username
# ./chat.py send target[,target ...]  message
#
# HOST_NAME="${HOSTNAME%%.*}"
# PID="$(pgrep -f './chat.py' | head -n1)"
# TOPIC="aiko/$HOST_NAME/$PID/1/in"
#
# mosquitto_pub -t $TOPIC -m "(send_message @all hello)"
#
# Notes
# ~~~~~
# targets: [names]: channels or @username: @all, @here
#
# To Do
# ~~~~~
# - UI: CLI (REPL), TUI (Dashboard plug-in), Web
#   - Implement /commands
# - Support multiple channels, multiple users
# - Security: ACLs (roles, users), encryption (shared symmetric keys) ?
# - Incorporate A.I Agents and Robots (real and virtual TUI/GUI)
#   - LLM with RAG based on chat history, other information sources (tools)

from abc import abstractmethod
import click

import aiko_services as aiko

_VERSION = 0

_ACTOR_TYPE = "chat"
_PROTOCOL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_TYPE}:{_VERSION}"

# --------------------------------------------------------------------------- #

class Chat(aiko.Actor):
    aiko.Interface.default("Chat", "aiko_chat.chat.ChatImpl")

    @abstractmethod
    def exit(self):
        pass

    @abstractmethod
    def send_message(self, targets, payload):
        pass

class ChatImpl(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.share["source_file"] = f"v{_VERSION}⇒ {__file__}"

    def exit(self):
        aiko.process.terminate()

    def send_message(self, targets, payload):
        self.logger.info(f"send_message({targets} {payload})")

# --------------------------------------------------------------------------- #

def get_service_filter():
    return aiko.ServiceFilter("*", _ACTOR_TYPE, _PROTOCOL, "*", "*", "*")

@click.group()

def main():
    """Run and exit Chat backend"""
    pass

@main.command(name="exit", help="Exit Chat backend")
def exit_command():
    aiko.do_command(Chat, get_service_filter(),
        lambda chat: chat.exit(), terminate=True)
    aiko.process.run()

@main.command(name="run")
def run_command():
    """Run Chat backend

    ./chat.py run
    """

    tags = ["ec=true"]       # TODO: Add ECProducer tag before add to Registrar
    init_args = aiko.actor_args(_ACTOR_TYPE, protocol=_PROTOCOL, tags=tags)
    chat = aiko.compose_instance(ChatImpl, init_args)
    aiko.process.run()

if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
