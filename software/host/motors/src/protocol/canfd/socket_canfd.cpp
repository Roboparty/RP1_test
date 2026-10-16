// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 wentywenty
// Copyright (C) 2026 Luo1imasi

/**
 * @file
 * This file implements functions to receive
 * and transmit CAN-FD frames via SocketCAN.
 */

#include "socket_canfd.hpp"

#include <cerrno>
#include <cctype>

#include <spdlog/sinks/stdout_color_sinks.h>

namespace {

bool is_fatal_canfd_error(int error) {
    return error == EBADF || error == ENETDOWN || error == ENETRESET ||
           error == ENODEV || error == ENXIO;
}

}  // namespace

// MotorsCANFD static members
std::shared_ptr<MotorsCANFD> MotorsCANFD::get(const std::string& interface, const std::string& backend) {
    ensure_logger();
    if (backend == "socketcan") {
        return MotorsSocketCANFD::get(interface);
    }
    throw std::runtime_error("Unknown CANFD backend: " + backend);
}

// MotorsSocketCANFD static members
std::unordered_map<std::string, std::weak_ptr<MotorsSocketCANFD>> MotorsSocketCANFD::instances_;
std::mutex MotorsSocketCANFD::instances_mutex_;

std::shared_ptr<MotorsSocketCANFD> MotorsSocketCANFD::get(const std::string& interface) {
    std::lock_guard<std::mutex> lock(instances_mutex_);
    auto found = instances_.find(interface);
    if (found != instances_.end()) {
        auto instance = found->second.lock();
        if (instance && instance->is_healthy()) {
            return instance;
        }
        instances_.erase(found);
    }

    auto instance = createInstance(interface);
    instances_[interface] = instance;
    return instance;
}

MotorsSocketCANFD::MotorsSocketCANFD(const std::string& interface)
    : interface_(interface), sockfd_(FD_INIT_FD), receiving_(false), tx_queue_(FD_TX_QUEUE_SIZE) {
    try {
        open(interface);
    } catch (...) {
        close();
        throw;
    }
}

MotorsSocketCANFD::~MotorsSocketCANFD() { this->close(); }

void MotorsSocketCANFD::open(const std::string& interface) {
    sockfd_ = socket(PF_CAN, SOCK_RAW, CAN_RAW);
    if (sockfd_ == FD_INIT_FD) {
        logger_->error("Failed to create CAN socket");
        throw std::runtime_error("Failed to create CAN socket");
    }

    int enable_canfd = 1;
    if (setsockopt(sockfd_, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &enable_canfd, sizeof(enable_canfd)) != 0) {
        logger_->error("Failed to enable CAN-FD support");
        this->close();
        throw std::runtime_error("Failed to enable CAN-FD support");
    }

    int bufsize = 1024 * 1024;  // 1MB
    setsockopt(sockfd_, SOL_SOCKET, SO_SNDBUF, &bufsize, sizeof(bufsize));

    strncpy(if_request_.ifr_name, interface.c_str(), IFNAMSIZ);
    if (ioctl(sockfd_, SIOCGIFINDEX, &if_request_) == -1) {
        logger_->error("Unable to detect CAN interface {}", interface);

        this->close();
        throw std::runtime_error("Unable to detect CAN interface " + interface);
    }

    // Bind the socket to the network interface
    addr_.can_family = AF_CAN;
    addr_.can_ifindex = if_request_.ifr_ifindex;
    int rc = ::bind(sockfd_, reinterpret_cast<struct sockaddr *>(&addr_), sizeof(addr_));
    if (rc == -1) {
        logger_->error("Failed to bind socket to network interface {}", interface);
        this->close();
        throw std::runtime_error("Failed to bind socket to network interface " + interface);
    }

    int flags = fcntl(sockfd_, F_GETFL, 0);
    if (flags == -1) {
        logger_->error("Failed to get socket flags");
        this->close();
        throw std::runtime_error("Failed to get socket flags");
    }
    if (fcntl(sockfd_, F_SETFL, flags | O_NONBLOCK) == -1) {
        logger_->error("Failed to set socket to non-blocking");
        this->close();
        throw std::runtime_error("Failed to set socket to non-blocking");
    }

    receiving_ = true;
    healthy_ = true;
    receiver_thread_ = std::thread([this]() {
        pthread_setname_np(pthread_self(), "canfd_rx");
        struct sched_param sp{}; sp.sched_priority = 48;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0) {
            logger_->error("Failed to set realtime priority for CANFD RX thread");
        }

        int total_cores = std::thread::hardware_concurrency();
        if (total_cores == 0) total_cores = 4; // Fallback
        const int little_cores = total_cores > 1 ? total_cores / 2 : 1;
        int cpu_id = 0;

        char last_char = interface_.back();
        if (isdigit(last_char)) {
            int port_num = last_char - '0';
            cpu_id = port_num % little_cores;
        }
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        CPU_SET(cpu_id, &cpuset);
        if (pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset) != 0) {
            logger_->error("Failed to bind CANFD RX thread to Core {}", cpu_id);
        }

        fd_set descriptors;
        int maxfd = sockfd_;
        struct timeval timeout;
        canfd_frame rx_frame;

        while (receiving_) {
            FD_ZERO(&descriptors);
            FD_SET(sockfd_, &descriptors);

            timeout.tv_sec = FD_TIMEOUT_SEC;
            timeout.tv_usec = FD_TIMEOUT_USEC;

            int sel_ret = ::select(maxfd + 1, &descriptors, NULL, NULL, &timeout);
            if (sel_ret < 0) {
                if (errno == EINTR) continue;
                const int select_error = errno;
                logger_->error("CANFD select error on {}: {}", interface_, strerror(select_error));
                invalidate();
                break;
            }
            if (sel_ret == 1) {
                while (true){
                    int len = ::read(sockfd_, &rx_frame, CANFD_MTU);
                    if (len < 0) {
                        if (errno == EAGAIN || errno == EWOULDBLOCK) {
                            break; 
                        }
                        const int read_error = errno;
                        logger_->warn("CANFD read error on {}: {}", interface_, strerror(read_error));
                        if (is_fatal_canfd_error(read_error)) {
                            invalidate();
                        }
                        break;
                    }
                    if (len == 0){
                        break;
                    }
                    {
                        std::lock_guard<std::mutex> lock(canfd_callback_mutex_);
                        CanFdCbkId key = key_extractor_(rx_frame);
                        auto it = canfd_callback_list_.find(key);
                        if (it != canfd_callback_list_.end()) {
                            it->second(rx_frame);
                        }
                    }
                }
            }
        }
    });

    sender_thread_ = std::thread([this]() {
        pthread_setname_np(pthread_self(), "canfd_tx");
        struct sched_param sp{}; sp.sched_priority = 48;
        if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp) != 0) {
            logger_->error("Failed to set realtime priority for CANFD TX thread");
        }

        int total_cores = std::thread::hardware_concurrency();
        if (total_cores == 0) total_cores = 4; // Fallback
        const int little_cores = total_cores > 1 ? total_cores / 2 : 1;
        int cpu_id = 0;
        
        char last_char = interface_.back();
        if (isdigit(last_char)) {
            int port_num = last_char - '0';
            cpu_id = port_num % little_cores;
        }
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        CPU_SET(cpu_id, &cpuset);
        if (pthread_setaffinity_np(pthread_self(), sizeof(cpu_set_t), &cpuset) != 0) {
            logger_->error("Failed to bind CANFD TX thread to Core {}", cpu_id);
        }

        canfd_frame tx_frame;
        int count = 0;
        while (receiving_) {
            {
                std::unique_lock<std::mutex> lock(tx_mutex_);
                tx_cv_.wait(lock, [this]() { return !tx_queue_.empty() || !receiving_; });
                if (!receiving_) break;
                if (!tx_queue_.pop(tx_frame)) continue;
            }
            ssize_t written;
            int write_error = 0;
            while ((written = ::write(sockfd_, &tx_frame, sizeof(canfd_frame))) < 0 &&
                   count < FD_MAX_RETRY_COUNT) {
                write_error = errno;
                if (is_fatal_canfd_error(write_error)) {
                    break;
                }
                count += 1;
                std::this_thread::sleep_for(std::chrono::microseconds(1000));
            }
            if (written < 0) {
                write_error = errno;
                logger_->error(
                    "Failed to transmit CAN-FD frame on {}: {}",
                    interface_,
                    strerror(write_error));
                if (is_fatal_canfd_error(write_error)) {
                    invalidate();
                    break;
                }
            } else if (send_sleep_us_ > 0) {
                std::this_thread::sleep_for(std::chrono::microseconds(send_sleep_us_));
            }
            count = 0;
        }
    });
}

void MotorsSocketCANFD::invalidate() {
    {
        std::lock_guard<std::mutex> lock(tx_mutex_);
        healthy_ = false;
        receiving_ = false;
    }
    tx_cv_.notify_all();
}

void MotorsSocketCANFD::close() {
    invalidate();
    if (receiver_thread_.joinable()) receiver_thread_.join();
    if (sender_thread_.joinable()) sender_thread_.join();

    if (sockfd_ != FD_INIT_FD) {
        if (::close(sockfd_) < 0) {
            if (logger_) logger_->warn("Failed to close socket {}: {}", interface_, strerror(errno));
        } else {
            if (logger_) logger_->info("CAN-FD interface {} closed successfully.", interface_);
        }
    }
    sockfd_ = FD_INIT_FD;
}

void MotorsSocketCANFD::transmit(const canfd_frame &frame) {
    bool queued = false;
    {
        std::lock_guard<std::mutex> lock(tx_mutex_);
        if (!receiving_) {
            logger_->error("Unable to transmit: Socket not open");
            return;
        }
        queued = tx_queue_.bounded_push(frame);
    }
    if (!queued) {
        logger_->error("CAN-FD transmit queue on {} is full; dropping frame 0x{:X}",
                       interface_, frame.can_id);
        return;
    }
    tx_cv_.notify_one();
}

void MotorsSocketCANFD::add_canfd_callback(const CanFdCbkFunc& callback, const CanFdCbkId id) {
    std::lock_guard<std::mutex> lock(canfd_callback_mutex_);
    canfd_callback_list_[id] = callback;
}

void MotorsSocketCANFD::remove_canfd_callback(CanFdCbkId id) {
    std::lock_guard<std::mutex> lock(canfd_callback_mutex_);
    canfd_callback_list_.erase(id);
}

void MotorsSocketCANFD::clear_canfd_callbacks() {
    std::lock_guard<std::mutex> lock(canfd_callback_mutex_);
    canfd_callback_list_.clear();
}

void MotorsSocketCANFD::set_canfd_key_extractor(CanFdCbkKeyExtractor extractor) {
    std::lock_guard<std::mutex> lock(canfd_callback_mutex_);
    key_extractor_ = std::move(extractor);
}
