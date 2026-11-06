#include "tracker_ipc.h"

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>

#include <dirent.h>
#include <sched.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#include <opencv2/opencv.hpp>
#include "Tracker_api.hpp"

namespace {


// Optional RK3588 scheduling policy. Configure the worker before the SDK starts
// its helper threads so video capture/encoding cannot be preempted by its nice
// -19 main thread. No algorithm, image processing or IPC behavior changes.
void configure_worker_cpus()
{
    const char *text = std::getenv("UAV_TRACKER_CPUS");
    if (!text || !*text) return;
    cpu_set_t requested;
    CPU_ZERO(&requested);
    const char *cursor = text;
    for (;;) {
        char *end = nullptr;
        errno = 0;
        const long cpu = std::strtol(cursor, &end, 10);
        if (errno || end == cursor || cpu < 0 || cpu >= CPU_SETSIZE ||
            (*end != ',' && *end != '\0')) {
            std::fprintf(stderr, "[tracker-worker] ignoring invalid UAV_TRACKER_CPUS=%s\n", text);
            return;
        }
        CPU_SET(static_cast<int>(cpu), &requested);
        if (!*end) break;
        cursor = end + 1;
    }
    if (sched_setaffinity(0, sizeof(requested), &requested) != 0) {
        std::fprintf(stderr, "[tracker-worker] CPU scheduling unchanged: %s\n", std::strerror(errno));
        return;
    }
    std::fprintf(stderr, "[tracker-worker] CPU affinity=%s\n", text);
}

TrackerIpcResult g_result{};
TrackerIpcResult g_last_detection{};
TrackerIpcResult g_detection_history[8]{};
int g_detection_history_count = 0;
bool g_tracker_registered = false;
bool g_unlock_submitted = false;
bool g_detection_enabled = true;
int g_frames_after_register = 0;
int g_last_reported_object_count = -1;
bool g_test_occlusion_enabled = false;
bool g_test_lock_requested = false;
int g_test_occlusion_stage = 0;
int g_test_occlusion_frames = 0;
int g_test_previous_status = 2;
TrackerIpcResult g_test_previous_result{};

int rounded_int(float value)
{
    return static_cast<int>(value >= 0.0f ? value + 0.5f : value - 0.5f);
}

unsigned short clamp_ushort(int value, int low, int high)
{
    int clamped = std::max(low, std::min(value, high));
    return static_cast<unsigned short>(
        std::min(clamped, static_cast<int>(std::numeric_limits<unsigned short>::max())));
}

unsigned int frame_id_u32(uint64_t frame_id)
{
    return static_cast<unsigned int>(
        std::min<uint64_t>(frame_id, static_cast<uint64_t>(UINT_MAX)));
}

void maybe_apply_occlusion_test(cv::Mat &bgr)
{
    if (!g_test_occlusion_enabled) return;
    if (g_test_occlusion_stage == 0 && g_test_previous_status == 3 &&
        g_test_previous_result.box_w > 0 && g_test_previous_result.box_h > 0) {
        g_test_occlusion_stage = 1;
        g_test_occlusion_frames = 30;
        std::fprintf(stderr, "[test] synthetic occlusion started on box=(%d,%d,%d,%d)\n",
                     g_test_previous_result.box_x, g_test_previous_result.box_y,
                     g_test_previous_result.box_w, g_test_previous_result.box_h);
    }
    if (g_test_occlusion_stage != 1) return;

    int margin_x = std::max(12, g_test_previous_result.box_w / 2);
    int margin_y = std::max(12, g_test_previous_result.box_h / 2);
    cv::Rect cover(g_test_previous_result.box_x - margin_x,
                   g_test_previous_result.box_y - margin_y,
                   g_test_previous_result.box_w + 2 * margin_x,
                   g_test_previous_result.box_h + 2 * margin_y);
    cover &= cv::Rect(0, 0, bgr.cols, bgr.rows);
    if (cover.area() > 0) cv::rectangle(bgr, cover, cv::Scalar(0, 0, 0), cv::FILLED);

    if (--g_test_occlusion_frames <= 0) {
        g_test_occlusion_stage = 2;
        std::fprintf(stderr, "[test] synthetic occlusion removed; live target visible again\n");
    }
}

bool has_valid_objects(const TrackerIpcResult &result)
{
    for (int i = 0; i < result.object_count; ++i) {
        if (result.objects[i].box_w > 0 && result.objects[i].box_h > 0) {
            return true;
        }
    }
    return false;
}

void result_callback(Tracker_Objects *result)
{
    if (!result) return;
    g_result.status = result->nTrackerStatus;
    g_result.box_x = rounded_int(result->xmin);
    g_result.box_y = rounded_int(result->ymin);
    g_result.box_w = std::max(0, rounded_int(result->w));
    g_result.box_h = std::max(0, rounded_int(result->h));

    int count = result->Objects
                    ? std::max(0, std::min(result->ntargetnum,
                                           TRACKER_IPC_MAX_OBJECTS))
                    : 0;
    g_result.object_count = count;
    for (int i = 0; i < count; ++i) {
        const Object_Property &source = result->Objects[i];
        TrackerIpcObject &target = g_result.objects[i];
        target.box_x = rounded_int(source.xmin);
        target.box_y = rounded_int(source.ymin);
        target.box_w = std::max(0, rounded_int(source.w));
        target.box_h = std::max(0, rounded_int(source.h));
        target.class_id = source.classId;
        target.track_id = source.track_id;
        target.score = source.score;
    }
    if (count != g_last_reported_object_count) {
        std::fprintf(stderr, "[tracker-worker] detected objects=%d\n", count);
        for (int i = 0; i < count; ++i) {
            const TrackerIpcObject &object = g_result.objects[i];
            std::fprintf(stderr,
                         "[tracker-worker] object[%d] class=%d id=%d box=(%d,%d,%d,%d) score=%.3f\n",
                         i, object.class_id, object.track_id,
                         object.box_x, object.box_y, object.box_w, object.box_h,
                         object.score);
        }
        g_last_reported_object_count = count;
    }
    // Keep the most recent non-empty detector result available for a
    // double-click command. RKNN inference can briefly return an empty frame
    // while the target is still visible, and dropping that frame would make
    // an otherwise valid click fail intermittently.
    if (count > 0 && has_valid_objects(g_result)) {
        g_last_detection = g_result;
        if (g_detection_history_count < 8) ++g_detection_history_count;
        for (int i = g_detection_history_count - 1; i > 0; --i) {
            g_detection_history[i] = g_detection_history[i - 1];
        }
        g_detection_history[0] = g_result;
    }
}

bool send_result(int socket_fd, int status, uint64_t frame_id)
{
    g_result.magic = TRACKER_IPC_MAGIC;
    g_result.status = status;
    g_result.frame_id = frame_id;
    return send(socket_fd, &g_result, sizeof(g_result), MSG_NOSIGNAL) ==
           static_cast<ssize_t>(sizeof(g_result));
}

void close_inherited_fds(int socket_fd, int memfd)
{
    DIR *directory = opendir("/proc/self/fd");
    if (!directory) return;
    int directory_fd = dirfd(directory);
    while (dirent *entry = readdir(directory)) {
        char *end = nullptr;
        long value = std::strtol(entry->d_name, &end, 10);
        if (!end || *end != '\0') continue;
        int fd = static_cast<int>(value);
        if (fd > STDERR_FILENO && fd != socket_fd && fd != memfd &&
            fd != directory_fd) {
            close(fd);
        }
    }
    closedir(directory);
}

bool chdir_tracker_build()
{
    char path[4096];
    ssize_t length = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (length <= 0) return false;
    path[length] = '\0';
    char *slash = std::strrchr(path, '/');
    if (!slash) return false;
    slash[1] = '\0';

    std::string build_dir = std::string(path) + "TTTracker_V1.1_publish/build";
    if (chdir(build_dir.c_str()) != 0) {
        std::fprintf(stderr, "[tracker-worker] chdir %s failed: %s\n",
                     build_dir.c_str(), std::strerror(errno));
        return false;
    }
    return true;
}

bool tracker_unlock(uint64_t frame_id)
{
    Tracker_Cmd command{};
    command.nLockMod = LOCK_MOD_MANUAL;
    command.nCmd = CMD_ID_UNLOCK;
    command.nFrameId = frame_id_u32(frame_id);
    return Tracker_SetCmd(command);
}

bool ensure_tracker_ready(const TrackerIpcCommand &command)
{
    if (!g_tracker_registered) {
        if (!Tracker_Register()) return false;
        g_tracker_registered = true;
        g_unlock_submitted = false;
        g_frames_after_register = 0;
        std::fprintf(stderr,
                     "[tracker-worker] TTTracker V1.1 registered pid=%d frame=%dx%d\n",
                     static_cast<int>(getpid()), command.width, command.height);
    }

    // Unlock on startup to enter multi-object search mode
    if (!g_unlock_submitted && g_frames_after_register >= 1) {
        if (!tracker_unlock(command.frame_id)) return false;
        g_unlock_submitted = true;
        std::fprintf(stderr,
                     "[tracker-worker] initial unlock submitted frame=%llu (SEARCH mode active)\n",
                     static_cast<unsigned long long>(command.frame_id));
    }
    return true;
}

void apply_command(const TrackerIpcCommand &command)
{
    if (command.cmd_type == TRACKER_CMD_NONE) return;

    Tracker_Cmd cmd{};
    cmd.nLockMod = LOCK_MOD_MANUAL;
    cmd.nFrameId = frame_id_u32(command.frame_id);

    if (command.cmd_type == TRACKER_CMD_UNLOCK) {
        cmd.nCmd = CMD_ID_UNLOCK;
        if (Tracker_SetCmd(cmd)) {
            std::fprintf(stderr, "[tracker-worker] CMD_UNLOCK applied frame=%llu\n",
                         static_cast<unsigned long long>(command.frame_id));
        } else {
            std::fprintf(stderr, "[tracker-worker] CMD_UNLOCK Tracker_SetCmd failed\n");
        }
    } else if (command.cmd_type == TRACKER_CMD_DETECTION) {
        g_detection_enabled = command.target_id != 0;
        std::memset(&g_result, 0, sizeof(g_result));
        if (!g_detection_enabled) {
            Tracker_Cmd unlock_cmd{};
            unlock_cmd.nLockMod = LOCK_MOD_MANUAL;
            unlock_cmd.nCmd = CMD_ID_UNLOCK;
            unlock_cmd.nFrameId = cmd.nFrameId;
            Tracker_SetCmd(unlock_cmd);
            std::memset(&g_last_detection, 0, sizeof(g_last_detection));
            std::memset(g_detection_history, 0, sizeof(g_detection_history));
            g_detection_history_count = 0;
            std::fprintf(stderr, "[tracker-worker] DETECTION disabled; boxes cleared\n");
        } else {
            std::fprintf(stderr, "[tracker-worker] DETECTION enabled\n");
        }
    } else if (command.cmd_type == TRACKER_CMD_LOCK_ID) {
        cmd.nCmd = CMD_ID_LOCK;
        cmd.nTargetId = static_cast<unsigned short>(command.target_id);

        bool found = false;
        const TrackerIpcResult *selection = &g_result;
        for (int pass = 0; pass < 10 && !found; ++pass) {
            if (pass == 0) selection = &g_result;
            else if (pass == 1) selection = &g_last_detection;
            else if (pass - 2 < g_detection_history_count) selection = &g_detection_history[pass - 2];
            else break;
            if (!has_valid_objects(*selection)) continue;
            for (int i = 0; i < selection->object_count; ++i) {
                const auto &object = selection->objects[i];
                if (object.track_id == command.target_id && object.box_w > 0 && object.box_h > 0) {
                    cmd.nLock_x = clamp_ushort(object.box_x, 0, command.width - 1);
                    cmd.nLock_y = clamp_ushort(object.box_y, 0, command.height - 1);
                    cmd.nLock_w = clamp_ushort(object.box_w, 1, command.width);
                    cmd.nLock_h = clamp_ushort(object.box_h, 1, command.height);
                    found = true;
                    break;
                }
            }
        }
        if (!found) {
            std::fprintf(stderr,
                         "[tracker-worker] CMD_LOCK_ID ignored: target ID=%d is not currently detected\n",
                         command.target_id);
            return;
        }

        if (Tracker_SetCmd(cmd)) {
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_ID: locked ID=%d box=(%d,%d,%d,%d) frame=%llu\n",
                         command.target_id, cmd.nLock_x, cmd.nLock_y, cmd.nLock_w, cmd.nLock_h,
                         static_cast<unsigned long long>(command.frame_id));
        } else {
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_ID Tracker_SetCmd failed for ID=%d\n",
                         command.target_id);
        }
    } else if (command.cmd_type == TRACKER_CMD_LOCK_XY) {
        // A ground-station double click is a point selection. Resolve it to
        // the detector's current tracked box; never synthesize a fixed box.
        const TrackerIpcResult *selection = has_valid_objects(g_result)
                                                ? &g_result
                                                : &g_last_detection;
        int hit_idx = -1;
        for (int i = 0; i < selection->object_count; ++i) {
            const auto &obj = selection->objects[i];
            if (obj.box_w <= 0 || obj.box_h <= 0) continue;
            if (command.click_x >= obj.box_x && command.click_x < obj.box_x + obj.box_w &&
                command.click_y >= obj.box_y && command.click_y < obj.box_y + obj.box_h) {
                hit_idx = i;
                break;
            }
        }
        // If not inside exact box, snap to nearest target if within 60px margin
        if (hit_idx < 0) {
            int best_dist_sq = 60 * 60;
            for (int i = 0; i < selection->object_count; ++i) {
                const auto &obj = selection->objects[i];
                if (obj.box_w <= 0 || obj.box_h <= 0) continue;
                int cx = obj.box_x + obj.box_w / 2;
                int cy = obj.box_y + obj.box_h / 2;
                int dx = command.click_x - cx;
                int dy = command.click_y - cy;
                int dist_sq = dx * dx + dy * dy;
                if (command.click_x >= obj.box_x - 60 && command.click_x <= obj.box_x + obj.box_w + 60 &&
                    command.click_y >= obj.box_y - 60 && command.click_y <= obj.box_y + obj.box_h + 60) {
                    if (dist_sq < best_dist_sq) {
                        best_dist_sq = dist_sq;
                        hit_idx = i;
                    }
                }
            }
        }

        cmd.nCmd = CMD_ID_LOCK;
        if (hit_idx >= 0) {
            const auto &obj = selection->objects[hit_idx];
            cmd.nTargetId = static_cast<unsigned short>(obj.track_id);
            cmd.nLock_x = clamp_ushort(obj.box_x, 0, command.width - 1);
            cmd.nLock_y = clamp_ushort(obj.box_y, 0, command.height - 1);
            cmd.nLock_w = clamp_ushort(obj.box_w, 1, command.width);
            cmd.nLock_h = clamp_ushort(obj.box_h, 1, command.height);
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_XY hit target #%d at (%d,%d) box=(%d,%d,%d,%d)\n",
                         obj.track_id, command.click_x, command.click_y,
                         cmd.nLock_x, cmd.nLock_y, cmd.nLock_w, cmd.nLock_h);
        } else {
            std::fprintf(stderr,
                         "[tracker-worker] CMD_LOCK_XY ignored: click (%d,%d) did not hit a detected box\n",
                         command.click_x, command.click_y);
            return;
        }

        if (Tracker_SetCmd(cmd)) {
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_XY locked successfully frame=%llu\n",
                         static_cast<unsigned long long>(command.frame_id));
        } else {
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_XY Tracker_SetCmd failed\n");
        }
    } else if (command.cmd_type == TRACKER_CMD_LOCK_BOX) {
        cmd.nCmd = CMD_ID_LOCK;
        cmd.nTargetId = 0;
        cmd.nLock_x = clamp_ushort(command.box_x, 0, command.width - 1);
        cmd.nLock_y = clamp_ushort(command.box_y, 0, command.height - 1);
        cmd.nLock_w = clamp_ushort(command.box_w, 1, command.width);
        cmd.nLock_h = clamp_ushort(command.box_h, 1, command.height);
        if (Tracker_SetCmd(cmd)) {
            std::fprintf(stderr, "[tracker-worker] CMD_LOCK_BOX locked box=(%d,%d,%d,%d) frame=%llu\n",
                         cmd.nLock_x, cmd.nLock_y, cmd.nLock_w, cmd.nLock_h,
                         static_cast<unsigned long long>(command.frame_id));
        }
    }
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc != 4) return 2;
    configure_worker_cpus();
    int socket_fd = std::atoi(argv[1]);
    int memfd = std::atoi(argv[2]);
    size_t capacity = static_cast<size_t>(std::strtoull(argv[3], nullptr, 10));
    const char *test_occlusion = std::getenv("TRACKER_TEST_OCCLUSION");
    g_test_occlusion_enabled = test_occlusion && std::strcmp(test_occlusion, "1") == 0;
    close_inherited_fds(socket_fd, memfd);
    void *mapped = mmap(nullptr, capacity, PROT_READ | PROT_WRITE, MAP_SHARED,
                        memfd, 0);
    if (mapped == MAP_FAILED) return 3;
    if (!chdir_tracker_build()) return 9;
    uint8_t *shared_rgb = static_cast<uint8_t *>(mapped);

    std::fprintf(stderr, "[tracker-worker] started with dynamic lock/unlock support\n");
    if (g_test_occlusion_enabled) {
        std::fprintf(stderr, "[test] occlusion simulation enabled; auto-selecting test car box\n");
    }

    for (;;) {
        TrackerIpcCommand command{};
        ssize_t count = recv(socket_fd, &command, sizeof(command), 0);
        if (count != static_cast<ssize_t>(sizeof(command)) ||
            command.magic != TRACKER_IPC_MAGIC) {
            return 0;
        }
        size_t bytes = static_cast<size_t>(command.width) * command.height * 3;
        if (command.width <= 0 || command.height <= 0 || bytes > capacity) {
            send_result(socket_fd, -2, command.frame_id);
            return 4;
        }

        try {
            cv::Mat rgb(command.height, command.width, CV_8UC3, shared_rgb,
                        static_cast<size_t>(command.width) * 3);
            cv::Mat bgr;
            cv::cvtColor(rgb, bgr, cv::COLOR_RGB2BGR);

            std::memset(&g_result, 0, sizeof(g_result));
            if (!ensure_tracker_ready(command)) {
                send_result(socket_fd, -3, command.frame_id);
                return 5;
            }

            // Apply interactive commands (Lock / Unlock)
            if (g_test_occlusion_enabled && !g_test_lock_requested &&
                command.cmd_type == TRACKER_CMD_NONE) {
                command.cmd_type = TRACKER_CMD_LOCK_BOX;
                command.box_x = 0;
                command.box_y = 1340;
                command.box_w = 205;
                command.box_h = 205;
                g_test_lock_requested = true;
                std::fprintf(stderr, "[test] issuing test lock box=(0,1340,205,205)\n");
            }
            apply_command(command);

            if (!g_detection_enabled) {
                g_result.magic = TRACKER_IPC_MAGIC;
                g_result.status = 2; // SEARCH while detection is disabled
                g_result.object_count = 0;
                ++g_frames_after_register;
                if (!send_result(socket_fd, g_result.status, command.frame_id)) return 0;
                continue;
            }

            maybe_apply_occlusion_test(bgr);

            if (!Tracker_CVMat_UpdateStatus(bgr, result_callback)) {
                send_result(socket_fd, -4, command.frame_id);
                return 6;
            }
            ++g_frames_after_register;


            static int last_status = -1;
            if (g_result.status != last_status) {
                std::fprintf(stderr, "[tracker-worker] state=%d frame=%llu\n",
                             g_result.status,
                             static_cast<unsigned long long>(command.frame_id));
                last_status = g_result.status;
            }

            // Tracker_StatusDraw owns the V1.1 recapture overlays: dashed
            // search regions and the current detector candidates.
            static bool recapture_draw_logged = false;
            if (g_result.status == TRACKER_STATUS_RECAP) {
                Tracker_StatusDraw(bgr, command.width, command.height);
                // rgb wraps the shared frame. Convert directly into it, then
                // signal completion; the streamer snapshots it before reuse.
                cv::cvtColor(bgr, rgb, cv::COLOR_BGR2RGB);
                if (!recapture_draw_logged) {
                    std::fprintf(stderr,
                                 "[tracker-worker] library RECAP overlay written to shared RGB frame=%llu %dx%d\n",
                                 static_cast<unsigned long long>(command.frame_id),
                                 command.width, command.height);
                    recapture_draw_logged = true;
                }
            } else {
                recapture_draw_logged = false;
            }

            // Detection targets are meaningful only in SEARCH. During TRACK,
            // RECAP, and LOST, never forward stale detector boxes downstream.
            if (g_result.status != 2) {
                g_result.object_count = 0;
                std::memset(g_result.objects, 0, sizeof(g_result.objects));
            }

            g_test_previous_status = g_result.status;
            g_test_previous_result = g_result;

            // Preserve RECAP/LOST from TTTracker.  The library state machine
            // owns the transition from tracking to recapture, recovery, or lost.
            if (!send_result(socket_fd, g_result.status, command.frame_id)) return 0;
        } catch (const cv::Exception &error) {
            std::fprintf(stderr, "[tracker-worker] OpenCV exception: %s\n", error.what());
            send_result(socket_fd, -5, command.frame_id);
            return 7;
        } catch (...) {
            send_result(socket_fd, -6, command.frame_id);
            return 8;
        }
    }
}
