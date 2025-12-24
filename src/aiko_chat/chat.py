#!/usr/bin/env python3
#
# Usage
# ~~~~~
# ./chat.py run
# ./chat.py exit
#
# ./chat.py repl username [channel]
# ./chat.py send recipient[,recipient ...]  message
#
# Usage: Low-level MQTT messages
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# HOST_NAME="${HOSTNAME%%.*}"
# PID="$(pgrep -f './chat.py' | head -n1)"
# TOPIC="aiko/$HOST_NAME/$PID/1/in"
#
# mosquitto_pub -t $TOPIC -m "(send_message @all hello)"
#
# Notes
# ~~~~~
# recipients: channel(s) or @username(s): @all, @here
#
# To Do
# ~~~~~
# - UI: CLI (REPL), TUI (Dashboard plug-in), Web
#   - Implement "/commands", e.g "/help"
#   - Refactor standard tty REPL ("scheme_tty.py")
# - Support multiple channels, multiple users
# - Security: ACLs (roles, users), encryption (shared symmetric keys) ?
# - Incorporate A.I Agents and Robots (real and virtual TUI/GUI)
#   - LLM with RAG based on chat history, other information sources (tools)

from abc import abstractmethod
import click
import signal

import aiko_services as aiko
from aiko_chat import FileHistoryStore, ReplSession

__all__ = ["ChatREPL", "ChatREPLImpl", "ChatServer", "ChatServerImpl"]

_HISTORY_PATHNAME = None
_VERSION = 0

_ACTOR_REPL = "chat_repl"
_PROTOCOL_REPL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_REPL}:{_VERSION}"

_ACTOR_SERVER = "chat_server"
_PROTOCOL_SERVER = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_SERVER}:{_VERSION}"

# --------------------------------------------------------------------------- #

def get_server_service_filter():
    return aiko.ServiceFilter(
        "*", _ACTOR_SERVER, _PROTOCOL_SERVER, "*", "*", "*")

def parse_recipients(recipients):
    if not recipients:
        return []
    return list(filter(None, map(str.strip, recipients.split(","))))

# --------------------------------------------------------------------------- #
# Aiko ChatREPL: Interface and Implementation

class ChatREPL(aiko.Actor):
    aiko.Interface.default("ChatREPL", "aiko_chat.chat.ChatREPLImpl")

class ChatREPLImpl(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.share["source_file"] = f"v{_VERSION}⇒ {__file__}"

        self.chat_server = None

        self.history_store = None
        if _HISTORY_PATHNAME:
            self.history_store = FileHistoryStore(_HISTORY_PATHNAME)
        self.repl_session = ReplSession(
            self.command_handler, history_store=self.history_store)
        signal.signal(signal.SIGINT, self.on_sigint)
        signal.signal(signal.SIGWINCH, self.on_sigwinch)
        self.repl_session.start(daemon=True)

        service_discovery, service_discovery_handler = aiko.do_discovery(
            ChatServer, get_server_service_filter(),
            self.discovery_add_handler, self.discovery_remove_handler)

    def command_handler(self, command, session):
        command = command.strip()
        if not command:
            return
        if command in ("/exit"):
            self.repl_session.stop()
            aiko.process.terminate()
        else:
            if self.chat_server:
                recipients = ["@all"]
                self.chat_server.send_message(recipients, command)

    def discovery_add_handler(self, service_details, service):
        self.print(f"Connected    {service_details[1]}: {service_details[0]}")
        self.chat_server = service

    def discovery_remove_handler(self, service_details):
        self.print(f"Disconnected {service_details[1]}: {service_details[0]}")
        self.chat_server = None

    def join(self):
        self.repl_session.join()  # wait until background thread has cleaned-up

    def on_sigint(self, signum, frame):
        self.repl_session.stop()
        aiko.process.terminate()

    def on_sigwinch(self, signum, frame):
        self.repl_session.request_resize()

    def print(self, output):
        self.repl_session.post_message(output)

# --------------------------------------------------------------------------- #
# Aiko ChatServer: Interface and Implementation

class ChatServer(aiko.Actor):
    aiko.Interface.default("ChatServer", "aiko_chat.chat.ChatServerImpl")

    @abstractmethod
    def exit(self):
        pass

    @abstractmethod
    def send_message(self, recipients, message):
        pass

class ChatServerImpl(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.share["source_file"] = f"v{_VERSION}⇒ {__file__}"

    def exit(self):
        aiko.process.terminate()

    def send_message(self, recipients, message):
        self.logger.info(f"send_message({recipients} {message})")

# --------------------------------------------------------------------------- #
# Aiko Chat CLI: Distributed Actor commands

@click.group()

def main():
    """Run and exit ChatServer backend"""
    pass

@main.command(name="exit", help="Exit ChatServer backend")
def exit_command():
    aiko.do_command(ChatServer, get_server_service_filter(),
        lambda chat: chat.exit(), terminate=True)
    aiko.process.run()

@main.command(name="repl")
def repl_command():
    """Run Chat CLI REPL frontend

    ./chat.py repl
    """

    tags = ["ec=true"]       # TODO: Add ECProducer tag before add to Registrar
    init_args = aiko.actor_args(_ACTOR_REPL, protocol=_PROTOCOL_REPL, tags=tags)
    chat = aiko.compose_instance(ChatREPLImpl, init_args)
    chat.print('Type "/exit" to exit')
    aiko.process.run()
    chat.join()  # wait until Chat ReplSession has cleaned-up

@main.command(name="run")
def run_command():
    """Run ChatServer backend

    ./chat.py run
    """

    tags = ["ec=true"]       # TODO: Add ECProducer tag before add to Registrar
    init_args = aiko.actor_args(
                    _ACTOR_SERVER, protocol=_PROTOCOL_SERVER, tags=tags)
    chat = aiko.compose_instance(ChatServerImpl, init_args)
    aiko.process.run()

@main.command(name="send")
@click.argument("recipients", type=str, required=True, default=None)
@click.argument("message", type=str, required=True, default=None)

def send_command(recipients, message):
    """Send message to recipients (channels and/or users)

    ./chat.py send RECIPIENTS MESSAGE

    \b
    • RECIPIENTS: List of one or more (comma separated) channels or @usernames
    • MESSAGE:    Data to be sent to the recipients
    """

    recipient_list = parse_recipients(recipients)
    aiko.do_command(ChatServer, get_server_service_filter(),
        lambda chat: chat.send_message(recipient_list, message), terminate=True)
    aiko.process.run()

if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
