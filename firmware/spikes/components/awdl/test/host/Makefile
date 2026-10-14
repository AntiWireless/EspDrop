# Native host build of the platform-independent AWDL frame unit test.
# No ESP-IDF required: pure C against the component's own headers.
#
# SPDX-License-Identifier: GPL-3.0-or-later

CC      ?= cc
CFLAGS  ?= -std=c11 -Wall -Wextra -Werror -O2 -g
COMP     = ../..
INCLUDE  = -I$(COMP)/include

SRCS = $(COMP)/awdl_frame.c test_awdl_frame.c
BIN  = test_awdl_frame

.PHONY: all run clean
all: run

$(BIN): $(SRCS) $(COMP)/include/awdl_frame.h $(COMP)/include/awdl_wire.h
	$(CC) $(CFLAGS) $(INCLUDE) $(SRCS) -o $(BIN)

run: $(BIN)
	./$(BIN)

clean:
	rm -f $(BIN)
