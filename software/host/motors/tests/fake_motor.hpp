// SPDX-License-Identifier: GPL-3.0
// Offline-only motor used by the uninstalled SDK concurrency test module.
#pragma once
#include "motor_driver.hpp"
#include <thread>
#include <chrono>

class FakeMotor final : public MotorDriver {
public:
    FakeMotor() { can_interface_ = "offline-test"; }
    void lock_motor() override {}
    void unlock_motor() override {}
    uint8_t init_motor() override { return 0; }
    void deinit_motor() override {}
    bool set_motor_zero() override {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        motor_pos_ = 0;
        return true;
    }
    bool write_motor_flash() override { return true; }
    void get_motor_param(uint8_t) override {}
    void motor_pos_cmd(float p, float, bool) override { motor_pos_ = p; }
    void motor_spd_cmd(float v) override { motor_spd_ = v; }
    void motor_mit_cmd(float p, float, float, float, float) override { motor_pos_ = p; }
    void motor_mit_cmd(float* p, float*, float*, float*, float*) override { motor_pos_ = p[0]; }
    void set_motor_control_mode(uint8_t v) override { motor_control_mode_ = v; }
    int get_response_count() const override { return 1; }
    void refresh_motor_status() override {}
    void clear_motor_error() override {}
    void set_motor_id(uint8_t, uint8_t v) override { motor_id_ = v; }
    void reset_motor_id() override { motor_id_ = 0; }
};
