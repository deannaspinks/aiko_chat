#!/usr/bin/env python3
#
# Usage
# ~~~~~
# ./chat.py run
# ./chat.py exit
#
# ./chat.py repl [username] [channel]
# ./chat.py send recipient[,recipient ...]  message
#
# Usage: Low-level MQTT messages
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# HOST_NAME="${HOSTNAME%%.*}"
# PID="$(pgrep -f './chat.py' | head -n1)"
# TOPIC="aiko/$HOST_NAME/$PID/1/in"
#
# mosquitto_pub -t $TOPIC -m "(send_message username @all hello)"
# Notes
# ~~~~~
# recipients: channel(s) or @username(s): @all, @here
#
# To Do
# ~~~~~
# *** Refactor LLM and Robot hacks
#     - Enable LLM, either ":llm_enable" or "#llm channel" message "enable" !
#     - Separate functions
#     - Provide simple conversational history
#     - Dynamically loaded "channel features" ?
#
# * Fix: Discover ChatServer via "owner" field ... support multiple concurrent
#   - Default search "owner" should be "*"
#   - Default "username" should be the "$USERNAME", override with REPL argument
#
# - Chat commands: MQTT pub/sub, do_command()/do_request to Service
#   - Connect Services/Actors via Dependencies and/or Categories ?
#
# - Support multiple channels via HyperSpace ?
#   * Create Category and Channels with the correct protocol type and owner
#   - What is stored in each Channel Dependency storage file ?
# - Support multiple users via HyperSpace ?
#   * Create Category and Users with the correct protocol type and owner
#   - What is stored in each User Dependency storage file ?
#
# - Implement "ChatServer.topic_out" Dependency link ...
#   - "ChatServer.topic_out" --[function_call]--> "ChatREPL.topic_in"
#
# - Add send_message() properties: timestamp, username
#
# - UI: CLI (REPL), TUI (Dashboard plug-in), Web
#   - Implement ":commands", e.g ":help" as dynamic plug-ins
#   - Refactor standard tty REPL ("scheme_tty.py") to use ReplSession ?
#
# - Incorporate A.I Agents and Robots (real and virtual TUI/GUI)
#   - LLM with RAG based on chat history, other information sources (tools)
#
# - Security: ACLs (roles, users), encryption (shared symmetric keys) ?

from abc import abstractmethod
import click
import signal
from typing import Iterable, List

import aiko_services as aiko
from aiko_services.examples.xgo_robot.robot import XGORobot
from aiko_chat import FileHistoryStore, ReplSession

__all__ = ["ChatREPL", "ChatREPLImpl", "ChatServer", "ChatServerImpl"]

_CHANNEL_NAME = "general"  # TODO: Support multiple channels (CRUD)
_HISTORY_PATHNAME = None
_HYPERSPACE_NAME = "chat_space"
_ROBOT_NAMES = ["laika", "oscar"]
_VERSION = 0

_ACTOR_REPL = "chat_repl"
_PROTOCOL_REPL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_REPL}:{_VERSION}"

_ACTOR_SERVER = "chat_server"
_PROTOCOL_SERVER = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_SERVER}:{_VERSION}"

# --------------------------------------------------------------------------- #

def get_server_service_filter():
    return aiko.ServiceFilter(
        "*", _ACTOR_SERVER, _PROTOCOL_SERVER, "*", "*", "*")

def generate_recipients(recipients: Iterable[str] | None) -> str:
    if not recipients:
        return ""
    return ",".join(recipient.strip() for recipient in recipients)

def parse_recipients(recipients: str | None) -> List[str]:
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

        self.current_channel = _CHANNEL_NAME
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

        self.print('Type ":exit" or ":x" to exit')
        self.print('Type ":help" or ":?" for instructions')
        self.print(f"Channel: {self.current_channel}")

    def command_handler(self, command_line, _repl_session):
        command_line = command_line.strip()
        if not command_line:
            return

        tokens = command_line.split(" ")
        command = tokens[0]
        if command in [":change_channel", ":cc"]:
            if len(tokens) > 1:
                self.current_channel = tokens[1]
                self.remove_message_handler(
                    self.server_message_handler, self.chat_server_topic)
                self.chat_server_topic =  \
                    f"{self.chat_server_topic_path}/{self.current_channel}"
                self.add_message_handler(
                    self.server_message_handler, self.chat_server_topic)
        elif command in [":exit", ":x"]:
            self.repl_session.stop()
            aiko.process.terminate()
        elif command in [":help", ":?"]:
            self.print(":change_channel, :cc  Change chat channel")
            self.print(":exit,           :x   Exit Chat")
            self.print(":help,           :?   Show instructions")
            self.print(":list_channels,  :lc  List chat channels")
        elif command in [":list_channels", ":lc"]:
            self.print("general, llm, random, robot, yolo")
        else:
            if self.chat_server:
                recipients = [self.current_channel]
                username = ""
                self.chat_server.send_message(
                    username, recipients, command_line)

    def discovery_add_handler(self, service_details, service):
        self.print(f"Connected {service_details[1]}: {service_details[0]}")
        self.chat_server = service
        self.chat_server_topic_path = service_details[0]
        self.chat_server_topic =  \
            f"{self.chat_server_topic_path}/{self.current_channel}"
        self.add_message_handler(
            self.server_message_handler, self.chat_server_topic)

    def discovery_remove_handler(self, service_details):
        self.print(f"Disconnected {service_details[1]}: {service_details[0]}")
        self.chat_server = None

    def join(self):
        self.repl_session.join()  # wait until background thread has cleaned-up

    def server_message_handler(self, _aiko, topic, payload_in):
        self.print(payload_in)

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
    def send_message(self, username, recipients, message):
        pass

class ChatServerImpl(aiko.Actor):
    def __init__(self, context, llm_enabled=False):
        context.call_init(self, "Actor", context)
        self.share["llm_enabled"] = llm_enabled
        self.share["source_file"] = f"v{_VERSION}⇒ {__file__}"

        self.hyperspace = aiko.HyperSpaceImpl.create_hyperspace(
            _HYPERSPACE_NAME)

        self.llm = None

        self.robot_server = None
        for name in _ROBOT_NAMES:
            service_discovery, service_discovery_handler = aiko.do_discovery(
                XGORobot, aiko.ServiceFilter("*", name, "*", "*", "*", "*"),
                self.discovery_add_handler, self.discovery_remove_handler)

    def discovery_add_handler(self, service_details, service):
        print(f"Connected    {service_details[1]}: {service_details[0]}")
        self.robot_server = service
        self.robot_server_topic = f"{service_details[0]}/in"

    def discovery_remove_handler(self, service_details):
        print(f"Disconnected {service_details[1]}: {service_details[0]}")
        self.robot_server = None

    def exit(self):
        aiko.process.terminate()

    def send_message(self, username, recipients, message):
        self.logger.info(f"send_message({username} > {recipients}: {message})")
        for recipient in recipients:
            recipient_topic_out = f"{self.topic_path}/{recipient}"
            aiko.process.message.publish(recipient_topic_out, message)

            is_sexpression = False
            sexp = message.strip()
            is_sexp = len(sexp) >= 2 and sexp[0] == "(" and sexp[-1] == ")"

            if recipient == "llm":
                response = "LLM is not enabled"
                if self.share["llm_enabled"]:
                    from httpx import ConnectError
                    from langchain_core.output_parsers import StrOutputParser
                    from langchain_core.prompts import ChatPromptTemplate
                    from aiko_services.examples.llm.elements import llm_load

                    SYSTEM_PROMPT = "Be brief."
                    chat_prompt = ChatPromptTemplate.from_messages([
                        ("system", SYSTEM_PROMPT), ("user", "{input}")])
                    llm = llm_load("ollama")
                    output_parser = StrOutputParser()

                    chain = chat_prompt | llm | output_parser
                    response = chain.invoke({"input": message})  # --> str

                aiko.process.message.publish(recipient_topic_out, response)

            if recipient == "robot" and self.robot_server:
                if is_sexp:
                    aiko.process.message.publish(self.robot_server_topic, sexp)
                else:
                    self.robot_server.action(message)

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
    aiko.process.run()
    chat.join()  # wait until Chat ReplSession has cleaned-up

@main.command(name="run")
@click.option("--llm", is_flag=True, help="Enable LLM (via ollama)")
def run_command(llm):
    """Run ChatServer backend

    ./chat.py run
    """

    tags = ["ec=true"]       # TODO: Add ECProducer tag before add to Registrar
    init_args = aiko.actor_args(
                    _ACTOR_SERVER, protocol=_PROTOCOL_SERVER, tags=tags)
    init_args["llm_enabled"] = llm
    chat = aiko.compose_instance(ChatServerImpl, init_args)
    aiko.process.run()

@main.command(name="send")
@click.argument("recipients", type=str, required=True, default=None)
@click.argument("message", type=str, required=True, default=None)

def send_command(recipients, message):
    """Send message to recipients (channels and/or users)

    ./chat.py send RECIPIENTS MESSAGE

    \b
    • RECIPIENTS: List of one or more (comma separated) #channels or @usernames
    • MESSAGE:    Data to be sent to the recipients
    """

    recipient_list = parse_recipients(recipients)
    username = ""
    aiko.do_command(ChatServer, get_server_service_filter(),
        lambda chat: chat.send_message(username, recipient_list, message),
        terminate=True)
    aiko.process.run()

if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
