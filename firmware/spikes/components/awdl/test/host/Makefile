# Native host build of the platform-independent unit tests.
# No ESP-IDF required: pure C against the component's own headers.
#
# SPDX-License-Identifier: GPL-3.0-or-later

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -O2 -g
COMP     = ../..
INCLUDE  = -I$(COMP)/include

# Platform-independent sources under test.
LIB = $(COMP)/awdl_frame.c $(COMP)/awdl_sync.c $(COMP)/awdl_election.c \
      $(COMP)/awdl_peers.c $(COMP)/awdl_node.c $(COMP)/awdl_cobs.c
HEADERS = $(wildcard $(COMP)/include/*.h)

BINS = test_awdl_frame test_awdl_cobs

.PHONY: all run clean
all: run

test_awdl_frame: $(LIB) test_awdl_frame.c $(HEADERS)
	$(CC) $(CFLAGS) $(INCLUDE) $(LIB) test_awdl_frame.c -o $@

test_awdl_cobs: $(LIB) test_awdl_cobs.c $(HEADERS)
	$(CC) $(CFLAGS) $(INCLUDE) $(LIB) test_awdl_cobs.c -o $@

run: $(BINS)
	./test_awdl_frame
	./test_awdl_cobs

clean:
	rm -f $(BINS)
