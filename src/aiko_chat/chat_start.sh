#!/usr/bin/env bash
#
# ./chat_start.sh
#
# To Do
# ~~~~~
# * Create Category and Channels with the correct protocol type and owner
# - What is stored in each Channel Dependency storage file ?
# * Create Category and Users with the correct protocol type and owner
# - What is stored in each User Dependency storage file ?
#
# - Catch errors ... clean-up and exit gracefully

CHAT_SERVER_PATH=_chat_server_
HYPERSPACE_PATH=_hyperspace_
CHANNELS_CATEGORY=channels
CHANNELS_LIST=general,random
USERS_CATEGORY=users

__initialize() {
  echo "### Creating $CHAT_SERVER_PATH"

  if [ ! -d $CHAT_SERVER_PATH ]; then
    mkdir -p $CHAT_SERVER_PATH
    echo "### Created  $CHAT_SERVER_PATH"
  fi
  cd "$CHAT_SERVER_PATH" || exit 1

  if [ ! -d $HYPERSPACE_PATH ]; then
    aiko_storage_file initialize
    echo "### Initialized Chat Server HyperSpace"
  fi

  if [ ! -L $CHANNELS_CATEGORY ]; then
    aiko_storage_file create --bootstrap $CHANNELS_CATEGORY

    IFS=',' read -r -a _CHANNELS <<< "$CHANNELS_LIST"
    for CHANNEL_NAME in "${_CHANNELS[@]}"; do
      if [ -n "$CHANNEL_NAME" ]; then
        if [ ! -L "$CHANNELS_CATEGORY/$CHANNEL_NAME" ]; then
          aiko_storage_file add --bootstrap $CHANNELS_CATEGORY/$CHANNEL_NAME
          echo "### Created $CHANNELS_CATEGORY/$CHANNEL_NAME"
        fi
      fi
    done
  fi

  if [ ! -L $USERS_CATEGORY ]; then
    aiko_storage_file create --bootstrap "$USERS_CATEGORY"
  fi

  aiko_storage_file list --bootstrap --long_format --recursive
}

__chat_server_run() {
  echo "### Running Chat Server"
  echo "... To stop Chat Server: aiko_chat exit"
  aiko_chat run
}

__initialize
__chat_server_run
