#pragma once

#include <stdint.h>

#define TRACKER_IPC_MAGIC 0x5454524bU
#define TRACKER_IPC_LOST 5

enum TrackerIpcStatus {
    TRACKER_STATUS_WAIT = 1,
    TRACKER_STATUS_SEARCH = 2,
    TRACKER_STATUS_TRACK = 3,
    TRACKER_STATUS_RECAP = 4,
    TRACKER_STATUS_LOST = 5,
};
#define TRACKER_IPC_MAX_OBJECTS 200

enum TrackerIpcCmdType {
    TRACKER_CMD_NONE = 0,
    TRACKER_CMD_LOCK_ID = 1,
    TRACKER_CMD_LOCK_XY = 2,
    TRACKER_CMD_UNLOCK = 3,
    TRACKER_CMD_LOCK_BOX = 4,
    TRACKER_CMD_DETECTION = 5,
};

struct TrackerIpcCommand {
    uint32_t magic;
    int32_t width;
    int32_t height;
    uint64_t frame_id;
    int32_t box_x;
    int32_t box_y;
    int32_t box_w;
    int32_t box_h;
    // Interactive Ground Station commands
    int32_t cmd_type;   // TrackerIpcCmdType
    int32_t target_id;  // For LOCK_ID
    int32_t click_x;    // For LOCK_XY
    int32_t click_y;    // For LOCK_XY
};

struct TrackerIpcObject {
    int32_t box_x;
    int32_t box_y;
    int32_t box_w;
    int32_t box_h;
    int32_t class_id;
    int32_t track_id;
    float score;
};

struct TrackerIpcResult {
    uint32_t magic;
    int32_t status;
    uint64_t frame_id;
    int32_t box_x;
    int32_t box_y;
    int32_t box_w;
    int32_t box_h;
    int32_t object_count;
    TrackerIpcObject objects[TRACKER_IPC_MAX_OBJECTS];
};
