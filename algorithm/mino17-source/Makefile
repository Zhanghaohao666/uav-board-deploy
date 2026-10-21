CC ?= gcc
.DEFAULT_GOAL := all
COMMON_DIR ?= ../../board/src
CFLAGS ?= -O2 -Wall -Wextra
CPPFLAGS += -I/usr/include/rockchip -I$(COMMON_DIR)
LDLIBS += -lrockchip_mpp -lrga -lpthread
OBJECTS = mino17_streamer.o mino_unpack.o camera.o mpp_encoder.o rtp_sender.o low_stream.o
low_stream.o: $(COMMON_DIR)/low_stream.c $(COMMON_DIR)/low_stream.h
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

all: mino17_streamer
mino17_streamer: $(OBJECTS)
test: test_unpack
	./test_unpack
test_unpack: test_unpack.c mino_unpack.c mino_unpack.h
	$(CC) $(CFLAGS) -o $@ test_unpack.c mino_unpack.c
clean:
	rm -f $(OBJECTS) mino17_streamer test_unpack
.PHONY: all test clean
