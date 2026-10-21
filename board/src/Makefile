CC ?= gcc
CFLAGS += -O2 -Wall -Wextra -I. -I/usr/include/rockchip
LDLIBS += -lrockchip_mpp -lrga -lpthread
SOURCES = streamer.c camera.c rga_convert.c pixel_convert.c mpp_encoder.c rtp_sender.c tracker_stub.c low_stream.c
rk_streamer: $(SOURCES) $(wildcard *.h)
	$(CC) $(CFLAGS) -o $@ $(SOURCES) $(LDLIBS)
rtsp_relay_mino17: rtsp_relay_mino17.c
	$(CC) -O2 -Wall -Wextra -o $@ $< $$(pkg-config --cflags --libs gstreamer-rtsp-server-1.0)
