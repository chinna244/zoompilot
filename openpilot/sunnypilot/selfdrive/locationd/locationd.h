#pragma once

#include <eigen3/Eigen/Dense>
#include <cstdint>
#include <deque>
#include <fstream>
#include <memory>
#include <map>
#include <string>

#include "cereal/messaging/messaging.h"
#include "common/params.h"
#include "common/swaglog.h"
#include "common/timing.h"
#include "common/util.h"

#include "sunnypilot/common/transformations/coordinates.hpp"
#include "sunnypilot/common/transformations/orientation.hpp"
#include "sunnypilot/system/sensord/sensors/constants.h"
#include "sunnypilot/selfdrive/locationd/models/live_kf.h"

#define VISION_DECIMATION 2
#define SENSOR_DECIMATION 10
#define POSENET_STD_HIST_HALF 20

enum LocalizerGnssSource {
  UBLOX, QCOM
};

// Passive diagnostics for GPS measurement accept/reject paths in handle_gps().
enum class GpsInputRejectReason : int32_t {
  None = 0,
  Accepted = 1,
  NoFix = 2,
  NonFiniteInput = 3,
  InvalidLatLonAlt = 4,
  InvalidHorizontalAccuracy = 5,
  InvalidVerticalAccuracy = 6,
  InvalidSpeedAccuracy = 7,
  InvalidBearingAccuracy = 8,
  UnreasonableUncertainty = 9,
  UnreasonableVelocity = 10,
  InvalidMeasurementTime = 11,
  FutureMeasurement = 12,
  StaleMeasurement = 13,
  PreTransitionMeasurement = 14,
};

struct GpsInputStats {
  uint64_t received = 0;
  uint64_t accepted = 0;
  uint64_t rejected_no_fix = 0;
  uint64_t rejected_non_finite = 0;
  uint64_t rejected_lat_lon_alt = 0;
  uint64_t rejected_horizontal_accuracy = 0;
  uint64_t rejected_vertical_accuracy = 0;
  uint64_t rejected_speed_accuracy = 0;
  uint64_t rejected_bearing_accuracy = 0;
  uint64_t rejected_unreasonable_uncertainty = 0;
  uint64_t rejected_unreasonable_velocity = 0;
  uint64_t rejected_invalid_measurement_time = 0;
  uint64_t rejected_future_measurement = 0;
  uint64_t rejected_stale_measurement = 0;
  uint64_t rejected_pre_transition_measurement = 0;
  GpsInputRejectReason last_reason = GpsInputRejectReason::None;
};

class Localizer {
public:
  Localizer(LocalizerGnssSource gnss_source = LocalizerGnssSource::UBLOX);

  int locationd_thread();

  void reset_kalman(double current_time = NAN);
  void reset_kalman(double current_time, const Eigen::VectorXd &init_orient, const Eigen::VectorXd &init_pos, const Eigen::VectorXd &init_vel, const MatrixXdr &init_pos_R, const MatrixXdr &init_vel_R);
  void reset_kalman(double current_time, const Eigen::VectorXd &init_x, const MatrixXdr &init_P);
  void finite_check(double current_time = NAN);
  void time_check(double current_time = NAN);
  void update_reset_tracker();
  bool is_gps_ok();
  bool critical_services_valid(const std::map<std::string, double> &critical_services);
  bool is_timestamp_valid(double current_time);
  void determine_gps_mode(double current_time);
  bool are_inputs_ok();
  void observation_timings_invalid_reset();

  kj::ArrayPtr<capnp::byte> get_message_bytes(MessageBuilder& msg_builder,
    bool inputsOK, bool sensorsOK, bool gpsOK, bool msgValid);
  void build_live_location(cereal::LiveLocationKalman::Builder& fix);

  Eigen::VectorXd get_position_geodetic();
  Eigen::VectorXd get_state();
  Eigen::VectorXd get_stdev();
  MatrixXdr get_cov();

  void handle_msg_bytes(const char *data, const size_t size);
  void handle_msg(const cereal::Event::Reader& log);
  void handle_sensor(double current_time, const cereal::SensorEventData::Reader& log);
  void handle_gps(double current_time, const cereal::GpsLocationData::Reader& log, const double sensor_time_offset);
  void handle_gnss(double current_time, const cereal::GnssMeasurements::Reader& log);
  void handle_car_state(double current_time, const cereal::CarState::Reader& log);
  void handle_cam_odo(double current_time, const cereal::CameraOdometry::Reader& log);
  void handle_live_calib(double current_time, const cereal::LiveCalibrationData::Reader& log);

  void input_fake_gps_observations(double current_time);

  // Passive GPS accept/reject diagnostics (no effect on filter behavior).
  const GpsInputStats &get_gps_input_stats() const { return this->gps_input_stats; }
  bool gps_course_used_for_last_reset() const { return this->last_reset_used_gps_course; }

private:
  std::unique_ptr<LiveKalman> kf;

  Eigen::VectorXd calib;
  MatrixXdr device_from_calib;
  MatrixXdr calib_from_device;
  bool calibrated = false;

  double car_speed = 0.0;
  double last_reset_time = NAN;
  std::deque<double> posenet_stds;

  std::unique_ptr<LocalCoord> converter;

  int64_t unix_timestamp_millis = 0;
  double reset_tracker = 0.0;
  bool device_fell = false;
  bool gps_mode = false;
  double first_valid_log_time = NAN;
  double ttff = NAN;
  double last_gps_msg = 0;
  LocalizerGnssSource gnss_source;
  // PR80: single authoritative source from gpsSourceState (no independent choice).
  enum class AuthGpsSource {
    UBLOX_PRIMARY,
    QCOM_FALLBACK,
    NO_HEALTHY_SOURCE,
  };
  // Effective authority after freshness gate (fail closed without fresh gpsSourceState).
  AuthGpsSource auth_gps_source = AuthGpsSource::NO_HEALTHY_SOURCE;
  // Last selected value from a received gpsSourceState (may be masked when stale).
  AuthGpsSource auth_gps_source_selected = AuthGpsSource::NO_HEALTHY_SOURCE;
  // Authority epoch from gpsSourceState.transitionMonoNs (integer ns for exact compare).
  uint64_t gps_source_transition_mono_ns = 0;
  double gps_source_transition_mono = 0.0;
  uint32_t gps_source_generation = 0;
  double gps_source_state_recv_mono = NAN;
  bool gps_source_state_seen = false;
  bool observation_timings_invalid = false;
  std::map<std::string, double> observation_values_invalid;
  bool standstill = true;
  int32_t orientation_reset_count = 0;
  float gps_std_factor;
  float gps_variance_factor;
  float gps_vertical_variance_factor;
  double gps_time_offset;
  Eigen::VectorXd camodo_yawrate_distribution = Eigen::Vector2d(0.0, 10.0); // mean, std

  GpsInputStats gps_input_stats;
  double last_gps_input_diag_log_t = NAN;
  bool last_reset_used_gps_course = false;

  void configure_gnss_source(const LocalizerGnssSource &source);
  void handle_gps_source_state(double current_time, const cereal::GpsSourceState::Reader &state);
  void refresh_gps_source_authority(double now_mono);
  bool gps_message_authoritative(AuthGpsSource expected_source, double msg_mono) const;
  void reject_gps_input(double current_time, GpsInputRejectReason reason);
  void maybe_log_gps_input_stats(double current_time);
  bool gps_course_usable_for_yaw_reset(double ecef_speed_mps, double bearing_accuracy_deg) const;
};
