# Native host build of the platform-independent AWDL frame unit test.
# No ESP-IDF required: pure C against the component's own headers.
#
# SPDX-License-Identifier: GPL-3.0-or-later

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -O2 -g
COMP     = ../..
INCLUDE  = -I$(COMP)/include

SRCS = $(COMP)/awdl_frame.c $(COMP)/awdl_sync.c $(COMP)/awdl_election.c \
       $(COMP)/awdl_peers.c test_awdl_frame.c
BIN  = test_awdl_frame
HEADERS = $(wildcard $(COMP)/include/*.h)

.PHONY: all run clean
all: run

$(BIN): $(SRCS) $(HEADERS)
	$(CC) $(CFLAGS) $(INCLUDE) $(SRCS) -o $(BIN)

run: $(BIN)
	./$(BIN)

clean:
	rm -f $(BIN)
