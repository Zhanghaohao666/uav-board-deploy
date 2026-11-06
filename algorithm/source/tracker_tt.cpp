#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include "tracker_stub.h"
#include "tracker_ipc.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/memfd.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

namespace {

constexpr int kTrackedCamId = 0;
constexpr int kRestartDelayMs = 500;
constexpr int kTcpPort = 9000;

#pragma pack(push, 1)
struct CmdHeader {
    uint8_t  magic;        // 0xAA
    uint8_t  cmd_id;       // Command ID
    uint16_t payload_len;  // Little-endian
};

struct StatusReportPayload {
    uint8_t  tracker_status;  // 1=WAIT, 2=SEARCH, 3=TRACK, 4=RECAP, 5=LOST
    int16_t  track_x;
    int16_t  track_y;
    int16_t  track_w;
    int16_t  track_h;
    int16_t  yaw_angle;       // 0
    int16_t  pitch_angle;     // 0
    uint8_t  target_count;
};

struct TargetInfoPayload {
    int16_t x;
    int16_t y;
    int16_t w;
    int16_t h;
    int32_t id;
};
#pragma pack(pop)

struct QueuedCmd {
    int cmd_type = 0;
    int target_id = 0;
    int click_x = 0;
    int click_y = 0;
    int box_x = 0;
    int box_y = 0;
    int box_w = 0;
    int box_h = 0;
};

std::mutex g_mutex;
pid_t g_worker_pid = -1;
int g_socket = -1;
int g_memfd = -1;
uint8_t *g_shared_rgb = nullptr;
size_t g_shared_size = 0;
bool g_worker_busy = false;
// A completed RECAP frame is snapshotted before the input buffer is reused.
// Pixels and the library overlays always belong to the same processed frame.
std::vector<uint8_t> g_recapture_rgb;
uint64_t g_recapture_frame_id = 0;
uint64_t g_frame_id = 0;
std::chrono::steady_clock::time_point g_restart_at{};
std::once_flag g_signal_once;
std::once_flag g_server_once;
volatile sig_atomic_t g_restart_requested = 0;

int g_status = 2; // SEARCH
bool g_have_box = false;
int g_box_x = 0;
int g_box_y = 0;
int g_box_w = 0;
int g_box_h = 0;
int g_object_count = 0;
TrackerIpcObject g_objects[TRACKER_IPC_MAX_OBJECTS]{};
int g_last_reported_object_count = -1;
std::atomic<bool> g_detection_enabled{true};

std::atomic<int> g_client_fd{-1};
std::mutex g_cmd_mutex;
QueuedCmd g_pending_cmd{};
bool g_has_pending_cmd = false;

void queue_cmd(int type, int target_id, int cx, int cy, int bx, int by, int bw, int bh)
{
    std::lock_guard<std::mutex> lock(g_cmd_mutex);
    g_pending_cmd.cmd_type = type;
    g_pending_cmd.target_id = target_id;
    g_pending_cmd.click_x = cx;
    g_pending_cmd.click_y = cy;
    g_pending_cmd.box_x = bx;
    g_pending_cmd.box_y = by;
    g_pending_cmd.box_w = bw;
    g_pending_cmd.box_h = bh;
    g_has_pending_cmd = true;
}

void tcp_server_worker()
{
    int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        std::fprintf(stderr, "[TCP-Server] socket() failed: %s\n", std::strerror(errno));
        return;
    }
    int opt = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(kTcpPort);

    if (bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::fprintf(stderr, "[TCP-Server] bind(%d) failed: %s\n", kTcpPort, std::strerror(errno));
        close(listen_fd);
        return;
    }
    if (listen(listen_fd, 4) != 0) {
        std::fprintf(stderr, "[TCP-Server] listen() failed: %s\n", std::strerror(errno));
        close(listen_fd);
        return;
    }

    std::fprintf(stderr, "[TCP-Server] Ground Station control server listening on port %d\n", kTcpPort);

    std::vector<uint8_t> rx_buf;
    rx_buf.reserve(4096);

    for (;;) {
        int cur_client = g_client_fd.load();
        pollfd pfd[2];
        pfd[0].fd = listen_fd;
        pfd[0].events = POLLIN;
        pfd[0].revents = 0;
        int nfds = 1;
        if (cur_client >= 0) {
            pfd[1].fd = cur_client;
            pfd[1].events = POLLIN | POLLHUP | POLLERR;
            pfd[1].revents = 0;
            nfds = 2;
        }

        int ret = poll(pfd, nfds, 100);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }

        if (pfd[0].revents & POLLIN) {
            sockaddr_in client_addr{};
            socklen_t client_len = sizeof(client_addr);
            int new_fd = accept(listen_fd, reinterpret_cast<sockaddr*>(&client_addr), &client_len);
            if (new_fd >= 0) {
                int flags = fcntl(new_fd, F_GETFL, 0);
                fcntl(new_fd, F_SETFL, flags | O_NONBLOCK);

                int old_fd = g_client_fd.exchange(new_fd);
                if (old_fd >= 0) {
                    close(old_fd);
                }
                rx_buf.clear();
                char ip_str[INET_ADDRSTRLEN];
                inet_ntop(AF_INET, &client_addr.sin_addr, ip_str, sizeof(ip_str));
                std::fprintf(stderr, "[TCP-Server] Ground Station connected from %s:%d\n",
                             ip_str, ntohs(client_addr.sin_port));
            }
        }

        if (nfds == 2 && (pfd[1].revents & (POLLIN | POLLHUP | POLLERR))) {
            cur_client = g_client_fd.load();
            if (cur_client < 0) continue;

            uint8_t tmp[1024];
            ssize_t n = recv(cur_client, tmp, sizeof(tmp), MSG_DONTWAIT);
            if (n > 0) {
                rx_buf.insert(rx_buf.end(), tmp, tmp + n);
                while (rx_buf.size() >= 4) {
                    if (rx_buf[0] != 0xAA) {
                        rx_buf.erase(rx_buf.begin());
                        continue;
                    }
                    uint8_t cmd_id = rx_buf[1];
                    uint16_t payload_len = static_cast<uint16_t>(rx_buf[2]) |
                                           (static_cast<uint16_t>(rx_buf[3]) << 8);
                    if (rx_buf.size() < 4 + payload_len) {
                        break;
                    }

                    const uint8_t *payload = rx_buf.data() + 4;
                    if (cmd_id == 0x01 && payload_len >= 4) {
                        // TRACK_XY
                        int16_t x = static_cast<int16_t>(payload[0] | (payload[1] << 8));
                        int16_t y = static_cast<int16_t>(payload[2] | (payload[3] << 8));
                        std::fprintf(stderr, "[TCP] Received TRACK_XY: (%d, %d)\n", x, y);
                        g_object_count = 0;
                        queue_cmd(TRACKER_CMD_LOCK_XY, 0, x, y, 0, 0, 0, 0);
                    } else if (cmd_id == 0x02 && payload_len >= 4) {
                        // TRACK_ID
                        int32_t tid;
                        std::memcpy(&tid, payload, 4);
                        std::fprintf(stderr, "[TCP] Received TRACK_ID: %d\n", tid);
                        g_object_count = 0;
                        queue_cmd(TRACKER_CMD_LOCK_ID, tid, 0, 0, 0, 0, 0, 0);
                    } else if (cmd_id == 0x03) {
                        // TRACK_UNLOCK
                        std::fprintf(stderr, "[TCP] Received TRACK_UNLOCK\n");
                        queue_cmd(TRACKER_CMD_UNLOCK, 0, 0, 0, 0, 0, 0, 0);
                    } else if (cmd_id == 0x04 && payload_len >= 8) {
                        // TRACK_BOX
                        int16_t x = static_cast<int16_t>(payload[0] | (payload[1] << 8));
                        int16_t y = static_cast<int16_t>(payload[2] | (payload[3] << 8));
                        int16_t w = static_cast<int16_t>(payload[4] | (payload[5] << 8));
                        int16_t h = static_cast<int16_t>(payload[6] | (payload[7] << 8));
                        std::fprintf(stderr, "[TCP] Received TRACK_BOX: (%d, %d, %d, %d)\n", x, y, w, h);
                        g_object_count = 0;
                        queue_cmd(TRACKER_CMD_LOCK_BOX, 0, 0, 0, x, y, w, h);
                    } else if (cmd_id == 0x05 && payload_len >= 1) {
                        // DETECTION_CONTROL: payload 1=enable, 0=disable
                        const bool enabled = payload[0] != 0;
                        g_detection_enabled.store(enabled);
                        if (!enabled) {
                            g_object_count = 0;
                            g_have_box = false;
                        }
                        std::fprintf(stderr, "[TCP] Received DETECTION_%s\n",
                                     enabled ? "ENABLE" : "DISABLE");
                        queue_cmd(TRACKER_CMD_DETECTION, enabled ? 1 : 0,
                                  0, 0, 0, 0, 0, 0);
                    } else if (cmd_id >= 0x10 && cmd_id <= 0x14) {
                        std::fprintf(stderr, "[TCP] Received gimbal command 0x%02x (ack)\n", cmd_id);
                    } else {
                        std::fprintf(stderr, "[TCP] Unknown command 0x%02x\n", cmd_id);
                    }

                    rx_buf.erase(rx_buf.begin(), rx_buf.begin() + 4 + payload_len);
                }
            } else if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                std::fprintf(stderr, "[TCP-Server] Ground Station disconnected\n");
                close(cur_client);
                g_client_fd.compare_exchange_strong(cur_client, -1);
                rx_buf.clear();
                queue_cmd(TRACKER_CMD_UNLOCK, 0, 0, 0, 0, 0, 0, 0);
            }
        }
    }
    close(listen_fd);
}

void broadcast_status_and_targets(int status, int box_x, int box_y, int box_w, int box_h,
                                  int obj_count, const TrackerIpcObject *objects)
{
    int client_fd = g_client_fd.load();
    if (client_fd < 0) return;

    // 1. STATUS_REPORT (0x80)
    uint8_t status_packet[4 + sizeof(StatusReportPayload)];
    CmdHeader *h1 = reinterpret_cast<CmdHeader*>(status_packet);
    h1->magic = 0xAA;
    h1->cmd_id = 0x80;
    h1->payload_len = sizeof(StatusReportPayload);

    StatusReportPayload *p1 = reinterpret_cast<StatusReportPayload*>(status_packet + 4);
    p1->tracker_status = static_cast<uint8_t>(status);
    p1->track_x = (status == 3) ? static_cast<int16_t>(box_x) : 0;
    p1->track_y = (status == 3) ? static_cast<int16_t>(box_y) : 0;
    p1->track_w = (status == 3) ? static_cast<int16_t>(box_w) : 0;
    p1->track_h = (status == 3) ? static_cast<int16_t>(box_h) : 0;
    p1->yaw_angle = 0;
    p1->pitch_angle = 0;
    const bool expose_detection_targets = g_detection_enabled.load() &&
                                          status == TRACKER_STATUS_SEARCH;
    uint8_t count = expose_detection_targets
                        ? static_cast<uint8_t>(std::min(obj_count, 32))
                        : 0;
    p1->target_count = count;

    ssize_t s1 = send(client_fd, status_packet, sizeof(status_packet), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (s1 < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
        close(client_fd);
        g_client_fd.compare_exchange_strong(client_fd, -1);
        return;
    }

    // 2. TARGET_LIST (0x81)
    size_t target_payload_len = 1 + static_cast<size_t>(count) * sizeof(TargetInfoPayload);
    std::vector<uint8_t> target_packet(4 + target_payload_len);
    CmdHeader *h2 = reinterpret_cast<CmdHeader*>(target_packet.data());
    h2->magic = 0xAA;
    h2->cmd_id = 0x81;
    h2->payload_len = static_cast<uint16_t>(target_payload_len);

    target_packet[4] = count;
    TargetInfoPayload *targets = reinterpret_cast<TargetInfoPayload*>(target_packet.data() + 5);
    for (int i = 0; i < count; ++i) {
        targets[i].x = static_cast<int16_t>(objects[i].box_x);
        targets[i].y = static_cast<int16_t>(objects[i].box_y);
        targets[i].w = static_cast<int16_t>(objects[i].box_w);
        targets[i].h = static_cast<int16_t>(objects[i].box_h);
        targets[i].id = objects[i].track_id;
    }

    ssize_t s2 = send(client_fd, target_packet.data(), target_packet.size(), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (s2 < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
        close(client_fd);
        g_client_fd.compare_exchange_strong(client_fd, -1);
    }
}

int env_int(const char *name, int fallback)
{
    const char *value = std::getenv(name);
    if (!value || !*value) return fallback;
    char *end = nullptr;
    long parsed = std::strtol(value, &end, 10);
    return (end != value && *end == '\0') ? static_cast<int>(parsed) : fallback;
}

int clamp_int(int value, int low, int high)
{
    return std::max(low, std::min(value, high));
}

void initial_box(int width, int height, TrackerIpcCommand *command)
{
    int raw_x = env_int("TRACKER_INIT_X", 460);
    int raw_y = env_int("TRACKER_INIT_Y", 860);
    int raw_w = env_int("TRACKER_INIT_W", 160);
    int raw_h = env_int("TRACKER_INIT_H", 200);
    const char *box_file = std::getenv("TRACKER_BOX_FILE");
    if (!box_file || !*box_file) box_file = "/tmp/rk_tracker_box";
    if (FILE *file = std::fopen(box_file, "r")) {
        int x, y, w, h;
        if (std::fscanf(file, "%d %d %d %d", &x, &y, &w, &h) == 4) {
            raw_x = x; raw_y = y; raw_w = w; raw_h = h;
        }
        std::fclose(file);
    }
    int x = clamp_int(raw_x, 0, std::max(0, width - 1));
    int y = clamp_int(raw_y, 0, std::max(0, height - 1));
    int w = clamp_int(raw_w, 16, std::max(16, width - x));
    int h = clamp_int(raw_h, 16, std::max(16, height - y));
    command->box_x = x;
    command->box_y = y;
    command->box_w = std::min(w, width - x);
    command->box_h = std::min(h, height - y);
}

std::string worker_path()
{
    char path[4096];
    ssize_t length = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (length <= 0) return "./tracker_worker";
    path[length] = '\0';
    char *slash = std::strrchr(path, '/');
    if (!slash) return "./tracker_worker";
    slash[1] = '\0';
    return std::string(path) + "tracker_worker";
}

void stop_worker(bool terminate)
{
    if (g_socket >= 0) close(g_socket);
    g_socket = -1;
    if (g_worker_pid > 0) {
        if (terminate) kill(g_worker_pid, SIGTERM);
        int status = 0;
        pid_t waited = waitpid(g_worker_pid, &status, WNOHANG);
        for (int attempt = 0; waited == 0 && attempt < 10; ++attempt) {
            usleep(5000);
            waited = waitpid(g_worker_pid, &status, WNOHANG);
        }
        if (waited == 0) {
            kill(g_worker_pid, SIGKILL);
            (void)waitpid(g_worker_pid, &status, 0);
        }
    }
    g_worker_pid = -1;
    g_worker_busy = false;
    g_recapture_rgb.clear();
    g_recapture_frame_id = 0;
}

bool prepare_shared_memory(size_t bytes)
{
    if (g_shared_rgb && g_shared_size == bytes) return true;

    stop_worker(true);
    if (g_shared_rgb) munmap(g_shared_rgb, g_shared_size);
    if (g_memfd >= 0) close(g_memfd);
    g_shared_rgb = nullptr;
    g_memfd = -1;
    g_shared_size = 0;

    g_memfd = static_cast<int>(syscall(SYS_memfd_create, "rk_tracker_rgb", 0));
    if (g_memfd < 0 || ftruncate(g_memfd, static_cast<off_t>(bytes)) != 0) {
        std::fprintf(stderr, "[tracker] cannot create shared frame buffer: %s\n",
                     std::strerror(errno));
        return false;
    }
    void *mapped = mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED,
                        g_memfd, 0);
    if (mapped == MAP_FAILED) {
        std::fprintf(stderr, "[tracker] cannot map shared frame buffer: %s\n",
                     std::strerror(errno));
        return false;
    }
    g_shared_rgb = static_cast<uint8_t *>(mapped);
    g_shared_size = bytes;
    return true;
}

bool start_worker()
{
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sockets) != 0) return false;

    std::string executable = worker_path();
    char socket_arg[32], memfd_arg[32], size_arg[32];
    std::snprintf(socket_arg, sizeof(socket_arg), "%d", sockets[1]);
    std::snprintf(memfd_arg, sizeof(memfd_arg), "%d", g_memfd);
    std::snprintf(size_arg, sizeof(size_arg), "%zu", g_shared_size);
    char *arguments[] = {const_cast<char *>(executable.c_str()), socket_arg,
                         memfd_arg, size_arg, nullptr};
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addclose(&actions, sockets[0]);
    pid_t pid = -1;
    int spawn_error = posix_spawn(&pid, executable.c_str(), &actions, nullptr,
                                  arguments, environ);
    posix_spawn_file_actions_destroy(&actions);

    close(sockets[1]);
    if (spawn_error != 0) {
        close(sockets[0]);
        std::fprintf(stderr, "[tracker] cannot start worker: %s\n",
                     std::strerror(spawn_error));
        return false;
    }
    g_worker_pid = pid;
    g_socket = sockets[0];
    g_worker_busy = false;
    std::fprintf(stderr, "[tracker] worker started pid=%d\n", static_cast<int>(pid));
    return true;
}

void schedule_restart(const char *reason)
{
    std::fprintf(stderr, "[tracker] %s; restarting worker in %d ms\n",
                 reason, kRestartDelayMs);
    stop_worker(true);
    g_have_box = false;
    g_object_count = 0;
    g_status = TRACKER_STATUS_SEARCH;
    g_restart_at = std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(kRestartDelayMs);
}

void restart_signal_handler(int)
{
    g_restart_requested = 1;
}

void receive_result()
{
    if (g_socket < 0 || !g_worker_busy) return;
    pollfd descriptor{g_socket, POLLIN | POLLHUP | POLLERR, 0};
    int ready = poll(&descriptor, 1, 0);
    if (ready <= 0) return;

    TrackerIpcResult result{};
    ssize_t count = recv(g_socket, &result, sizeof(result), MSG_DONTWAIT);
    if (count != static_cast<ssize_t>(sizeof(result)) ||
        result.magic != TRACKER_IPC_MAGIC || result.frame_id != g_frame_id) {
        schedule_restart("worker exited unexpectedly");
        return;
    }
    g_worker_busy = false;

    if (result.status < 0) {
        schedule_restart("worker reported an error");
        return;
    }

    if (result.status == TRACKER_STATUS_RECAP && g_shared_rgb) {
        const bool entering_recapture = g_recapture_rgb.empty();
        g_recapture_rgb.resize(g_shared_size);
        std::memcpy(g_recapture_rgb.data(), g_shared_rgb, g_shared_size);
        g_recapture_frame_id = result.frame_id;
        if (entering_recapture) {
            std::fprintf(stderr,
                         "[tracker] forwarding library RECAP RGB frame=%llu bytes=%zu\n",
                         static_cast<unsigned long long>(g_recapture_frame_id),
                         g_shared_size);
        }
    } else {
        g_recapture_rgb.clear();
        g_recapture_frame_id = 0;
    }

    g_status = result.status;
    g_box_x = result.box_x;
    g_box_y = result.box_y;
    g_box_w = result.box_w;
    g_box_h = result.box_h;
    g_have_box = (g_status == 3) && (g_box_w > 0) && (g_box_h > 0);

    g_object_count = clamp_int(result.object_count, 0, TRACKER_IPC_MAX_OBJECTS);
    if (g_object_count > 0) {
        std::memcpy(g_objects, result.objects,
                    static_cast<size_t>(g_object_count) * sizeof(g_objects[0]));
    }
    if (g_object_count != g_last_reported_object_count) {
        std::fprintf(stderr, "[tracker] multi-object boxes=%d status=%d\n", g_object_count, g_status);
        g_last_reported_object_count = g_object_count;
    }
}

void draw_rectangles(uint8_t *rgb, int width, int height)
{
    if (!rgb || width <= 0 || height <= 0) return;
    cv::Mat mat(height, width, CV_8UC3, rgb);

    // Detection boxes are only drawn in SEARCH mode. TRACK mode draws the
    // single tracker box below, so the last multi-object frame cannot remain.
    if (g_detection_enabled.load() && g_status == TRACKER_STATUS_SEARCH) {
      for (int index = 0; index < g_object_count; ++index) {
        const TrackerIpcObject &object = g_objects[index];
        if (object.box_w <= 0 || object.box_h <= 0) continue;

        cv::Scalar color(255, 64, 255);
        const char *class_name = "Obj";
        if (object.class_id == 0) {
            color = cv::Scalar(255, 64, 64);   // Red: Person
            class_name = "Person";
        } else if (object.class_id == 1) {
            color = cv::Scalar(64, 128, 255); // Blue: Car
            class_name = "Car";
        } else if (object.class_id == 2) {
            color = cv::Scalar(255, 255, 0);  // Yellow: Drone
            class_name = "Drone";
        }

        int x0 = std::max(0, std::min(object.box_x, width - 1));
        int y0 = std::max(0, std::min(object.box_y, height - 1));
        int w = std::max(0, std::min(object.box_w, width - x0));
        int h = std::max(0, std::min(object.box_h, height - y0));
        if (w <= 0 || h <= 0) continue;

        cv::Rect rect(x0, y0, w, h);
        cv::rectangle(mat, rect, color, 2);

        char label[64];
        std::snprintf(label, sizeof(label), "#%d %s", object.track_id, class_name);

        int baseline = 0;
        double font_scale = 0.75;
        int thickness = 2;
        cv::Size text_size = cv::getTextSize(label, cv::FONT_HERSHEY_SIMPLEX,
                                            font_scale, thickness, &baseline);

        int badge_h = text_size.height + baseline + 6;
        int badge_w = text_size.width + 10;
        int badge_x = x0;
        if (badge_x + badge_w > width) badge_x = std::max(0, width - badge_w);

        int badge_y = (y0 >= badge_h) ? (y0 - badge_h) : y0;

        cv::Rect badge_rect(badge_x, badge_y, badge_w, badge_h);
        cv::rectangle(mat, badge_rect, color, cv::FILLED);

        cv::Scalar text_color = (object.class_id == 2) ? cv::Scalar(0, 0, 0)
                                                       : cv::Scalar(255, 255, 255);
        cv::Point text_origin(badge_x + 5, badge_y + text_size.height + 3);
        cv::putText(mat, label, text_origin, cv::FONT_HERSHEY_SIMPLEX,
                    font_scale, text_color, thickness, cv::LINE_AA);
      }
    }

    // Highlight Single-Target Locked Tracking Box (Status == 3)
    if (g_have_box && g_box_w > 0 && g_box_h > 0) {
        int x0 = std::max(0, std::min(g_box_x, width - 1));
        int y0 = std::max(0, std::min(g_box_y, height - 1));
        int w = std::max(0, std::min(g_box_w, width - x0));
        int h = std::max(0, std::min(g_box_h, height - y0));
        if (w > 0 && h > 0) {
            cv::Scalar lock_color(0, 255, 0); // Bright Green
            cv::rectangle(mat, cv::Rect(x0, y0, w, h), lock_color, 4);

            const char *lock_label = "[LOCKED TRACKING]";
            int baseline = 0;
            double font_scale = 0.85;
            int thickness = 2;
            cv::Size text_size = cv::getTextSize(lock_label, cv::FONT_HERSHEY_SIMPLEX,
                                                font_scale, thickness, &baseline);
            int badge_h = text_size.height + baseline + 8;
            int badge_w = text_size.width + 12;
            int badge_x = x0;
            if (badge_x + badge_w > width) badge_x = std::max(0, width - badge_w);
            int badge_y = (y0 >= badge_h) ? (y0 - badge_h) : y0;

            cv::rectangle(mat, cv::Rect(badge_x, badge_y, badge_w, badge_h), lock_color, cv::FILLED);
            cv::putText(mat, lock_label, cv::Point(badge_x + 6, badge_y + text_size.height + 4),
                        cv::FONT_HERSHEY_SIMPLEX, font_scale, cv::Scalar(0, 0, 0), thickness, cv::LINE_AA);
        }
    }
}

void render_output_frame(uint8_t *rgb, int width, int height)
{
    const size_t bytes = static_cast<size_t>(width) * height * 3;
    if (g_detection_enabled.load() && g_status == TRACKER_STATUS_RECAP &&
        g_recapture_frame_id != 0 && g_recapture_rgb.size() == bytes) {
        // Display the exact completed frame until another result arrives.
        // Never put old overlay pixels on a newer unrelated camera frame.
        std::memcpy(rgb, g_recapture_rgb.data(), bytes);
        return;
    }
    draw_rectangles(rgb, width, height);
}

}  // namespace

extern "C" void tracker_process(uint8_t *rgb, int width, int height, int cam_id)
{
    if (cam_id != kTrackedCamId || !rgb || width <= 0 || height <= 0) return;
    std::call_once(g_signal_once, [] { signal(SIGUSR1, restart_signal_handler); });
    std::call_once(g_server_once, [] {
        std::thread(tcp_server_worker).detach();
    });
    std::lock_guard<std::mutex> lock(g_mutex);

    size_t bytes = static_cast<size_t>(width) * height * 3;
    if (!prepare_shared_memory(bytes)) return;

    if (g_restart_requested) {
        g_restart_requested = 0;
        schedule_restart("manual restart requested");
    }

    receive_result();
    broadcast_status_and_targets(g_status, g_box_x, g_box_y, g_box_w, g_box_h,
                                 g_object_count, g_objects);
    if (g_socket < 0) {
        if (std::chrono::steady_clock::now() < g_restart_at) {
            render_output_frame(rgb, width, height);
            return;
        }
        if (!start_worker()) {
            g_restart_at = std::chrono::steady_clock::now() +
                           std::chrono::milliseconds(1000);
            render_output_frame(rgb, width, height);
            return;
        }
    }
    if (g_worker_busy) {
        render_output_frame(rgb, width, height);
        return;
    }

    // Submit the pristine camera input before modifying the outgoing video.
    std::memcpy(g_shared_rgb, rgb, bytes);
    TrackerIpcCommand command{};
    command.magic = TRACKER_IPC_MAGIC;
    command.width = width;
    command.height = height;
    command.frame_id = ++g_frame_id;
    initial_box(width, height, &command);

    // Apply any pending commands from Ground Station
    {
        std::lock_guard<std::mutex> cmd_lock(g_cmd_mutex);
        if (g_has_pending_cmd) {
            command.cmd_type = g_pending_cmd.cmd_type;
            command.target_id = g_pending_cmd.target_id;
            command.click_x = g_pending_cmd.click_x;
            command.click_y = g_pending_cmd.click_y;
            if (g_pending_cmd.cmd_type == TRACKER_CMD_LOCK_BOX) {
                command.box_x = g_pending_cmd.box_x;
                command.box_y = g_pending_cmd.box_y;
                command.box_w = g_pending_cmd.box_w;
                command.box_h = g_pending_cmd.box_h;
            }
            g_has_pending_cmd = false;
        } else {
            command.cmd_type = TRACKER_CMD_NONE;
        }
    }

    ssize_t sent = send(g_socket, &command, sizeof(command), MSG_NOSIGNAL);
    if (sent != static_cast<ssize_t>(sizeof(command))) {
        schedule_restart("cannot submit frame to worker");
        render_output_frame(rgb, width, height);
        return;
    }
    g_worker_busy = true;
    render_output_frame(rgb, width, height);
}
