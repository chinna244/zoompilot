#define CATCH_CONFIG_MAIN
#include "catch2/catch.hpp"

#include <cmath>
#include <limits>

#include "cereal/messaging/messaging.h"
#include "sunnypilot/common/transformations/coordinates.hpp"
#include "sunnypilot/common/transformations/orientation.hpp"
#include "sunnypilot/selfdrive/locationd/locationd.h"
#include "sunnypilot/selfdrive/locationd/models/live_kf.h"

using namespace Eigen;

namespace {

constexpr double kValidLat = 37.3861;
constexpr double kValidLon = -122.0839;
constexpr double kValidAlt = 10.0;
constexpr double kSensorOffset = 0.095;

struct GpsFields {
  bool has_fix = true;
  double latitude = kValidLat;
  double longitude = kValidLon;
  double altitude = kValidAlt;
  float bearing_deg = 90.0f;
  float horizontal_accuracy = 2.0f;
  float vertical_accuracy = 3.0f;
  float speed_accuracy = 0.5f;
  float bearing_accuracy_deg = 5.0f;
  float vn = 0.0f;
  float ve = 0.0f;
  float vd = 0.0f;
};

void fill_gps(cereal::GpsLocationData::Builder gps, const GpsFields &f) {
  gps.setHasFix(f.has_fix);
  gps.setLatitude(f.latitude);
  gps.setLongitude(f.longitude);
  gps.setAltitude(f.altitude);
  gps.setBearingDeg(f.bearing_deg);
  gps.setHorizontalAccuracy(f.horizontal_accuracy);
  gps.setVerticalAccuracy(f.vertical_accuracy);
  gps.setSpeedAccuracy(f.speed_accuracy);
  gps.setBearingAccuracyDeg(f.bearing_accuracy_deg);
  gps.setUnixTimestampMillis(1'700'000'000'000LL);
  auto vned = gps.initVNED(3);
  vned.set(0, f.vn);
  vned.set(1, f.ve);
  vned.set(2, f.vd);
}

void seed_filter_near_gps(Localizer &loc, double t, const GpsFields &f) {
  Geodetic geo = {f.latitude, f.longitude, f.altitude};
  ECEF ecef = geodetic2ecef(geo);
  VectorXd x = loc.get_state();
  MatrixXdr P = loc.get_cov();
  x.segment<STATE_ECEF_POS_LEN>(STATE_ECEF_POS_START) = Vector3d(ecef.x, ecef.y, ecef.z);
  loc.reset_kalman(t, x, P);
}

VectorXd quat_to_vector(const Quaterniond &quat) {
  return Vector4d(quat.w(), quat.x(), quat.y(), quat.z());
}

}  // namespace

TEST_CASE("finite_check requires both x and P finite", "[pr75][finite]") {
  Localizer loc;
  const double t = 10.0;
  loc.reset_kalman(t);

  SECTION("x finite, P finite -> no reset") {
    VectorXd x_before = loc.get_state();
    loc.finite_check(t);
    REQUIRE(loc.get_state().isApprox(x_before));
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }

  SECTION("x NaN, P finite -> reset") {
    VectorXd x = loc.get_state();
    MatrixXdr P = loc.get_cov();
    x(STATE_ECEF_POS_START) = std::numeric_limits<double>::quiet_NaN();
    loc.reset_kalman(t, x, P);
    REQUIRE_FALSE(loc.get_state().array().isFinite().all());
    loc.finite_check(t + 1.0);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }

  SECTION("x finite, P NaN -> reset") {
    VectorXd x = loc.get_state();
    MatrixXdr P = loc.get_cov();
    P(0, 0) = std::numeric_limits<double>::quiet_NaN();
    loc.reset_kalman(t, x, P);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE_FALSE(loc.get_cov().array().isFinite().all());
    loc.finite_check(t + 1.0);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }

  SECTION("x Inf, P finite -> reset") {
    VectorXd x = loc.get_state();
    MatrixXdr P = loc.get_cov();
    x(STATE_ECEF_POS_START) = std::numeric_limits<double>::infinity();
    loc.reset_kalman(t, x, P);
    loc.finite_check(t + 1.0);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }

  SECTION("x finite, P Inf -> reset") {
    VectorXd x = loc.get_state();
    MatrixXdr P = loc.get_cov();
    P(0, 0) = std::numeric_limits<double>::infinity();
    loc.reset_kalman(t, x, P);
    loc.finite_check(t + 1.0);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }

  SECTION("both invalid -> reset") {
    VectorXd x = loc.get_state();
    MatrixXdr P = loc.get_cov();
    x(STATE_ECEF_POS_START) = std::numeric_limits<double>::quiet_NaN();
    P(0, 0) = std::numeric_limits<double>::infinity();
    loc.reset_kalman(t, x, P);
    loc.finite_check(t + 1.0);
    REQUIRE(loc.get_state().array().isFinite().all());
    REQUIRE(loc.get_cov().array().isFinite().all());
  }
}

TEST_CASE("handle_gps rejects non-finite numeric inputs before fusion", "[pr75][gps][nonfinite]") {
  Localizer loc;
  loc.reset_kalman(1.0);
  GpsFields base;
  seed_filter_near_gps(loc, 1.0, base);

  auto require_reject = [&](GpsFields f, GpsInputRejectReason reason) {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    const uint64_t accepted_before = loc.get_gps_input_stats().accepted;
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == reason);
    REQUIRE(loc.get_gps_input_stats().accepted == accepted_before);
    REQUIRE(loc.get_gps_input_stats().rejected_non_finite >= 1);
  };

  SECTION("latitude NaN") {
    GpsFields f = base;
    f.latitude = std::numeric_limits<double>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("longitude Inf") {
    GpsFields f = base;
    f.longitude = std::numeric_limits<double>::infinity();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("altitude NaN") {
    GpsFields f = base;
    f.altitude = std::numeric_limits<double>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("horizontalAccuracy NaN") {
    GpsFields f = base;
    f.horizontal_accuracy = std::numeric_limits<float>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("verticalAccuracy Inf") {
    GpsFields f = base;
    f.vertical_accuracy = std::numeric_limits<float>::infinity();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("speedAccuracy NaN") {
    GpsFields f = base;
    f.speed_accuracy = std::numeric_limits<float>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("bearingAccuracy NaN") {
    GpsFields f = base;
    f.bearing_accuracy_deg = std::numeric_limits<float>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("bearingDeg Inf") {
    GpsFields f = base;
    f.bearing_deg = std::numeric_limits<float>::infinity();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("vNED north NaN") {
    GpsFields f = base;
    f.vn = std::numeric_limits<float>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("vNED east Inf") {
    GpsFields f = base;
    f.ve = std::numeric_limits<float>::infinity();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("vNED down NaN") {
    GpsFields f = base;
    f.vd = std::numeric_limits<float>::quiet_NaN();
    require_reject(f, GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("non-finite current_time") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, base);
    loc.handle_gps(std::numeric_limits<double>::quiet_NaN(), gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("non-finite sensor_time_offset") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, base);
    const uint64_t accepted_before = loc.get_gps_input_stats().accepted;
    loc.handle_gps(2.0, gps.asReader(), std::numeric_limits<double>::quiet_NaN());
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NonFiniteInput);
    REQUIRE(loc.get_gps_input_stats().accepted == accepted_before);
  }
}

TEST_CASE("handle_gps UBLOX horizontal accuracy semantics", "[pr75][gps][accuracy][ublox]") {
  Localizer loc(LocalizerGnssSource::UBLOX);
  loc.reset_kalman(1.0);
  GpsFields base;
  seed_filter_near_gps(loc, 1.0, base);

  SECTION("finite positive horizontalAccuracy accepted") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, base);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
  }
  SECTION("zero horizontalAccuracy rejected") {
    GpsFields f = base;
    f.horizontal_accuracy = 0.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidHorizontalAccuracy);
  }
  SECTION("negative horizontalAccuracy rejected") {
    GpsFields f = base;
    f.horizontal_accuracy = -1.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidHorizontalAccuracy);
  }
  SECTION("NaN horizontalAccuracy rejected as non-finite") {
    GpsFields f = base;
    f.horizontal_accuracy = std::numeric_limits<float>::quiet_NaN();
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NonFiniteInput);
  }
  SECTION("Inf horizontalAccuracy rejected as non-finite") {
    GpsFields f = base;
    f.horizontal_accuracy = std::numeric_limits<float>::infinity();
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NonFiniteInput);
  }
}

TEST_CASE("QCOM requires positive horizontalAccuracy for covariance", "[pr75][pr81][gps][qcom]") {
  // PR81 uses H and V in NED→ECEF covariance for all sources; zero H is reject.
  Localizer loc(LocalizerGnssSource::QCOM);
  loc.reset_kalman(1.0);
  GpsFields f;
  f.horizontal_accuracy = 0.0f;
  f.vertical_accuracy = 1.0f;
  f.speed_accuracy = 1.0f;
  f.bearing_accuracy_deg = 1.0f;
  seed_filter_near_gps(loc, 1.0, f);

  MessageBuilder msg;
  auto gps = msg.initEvent().initGpsLocation();
  fill_gps(gps, f);
  loc.handle_gps(2.0, gps.asReader(), 0.630);
  REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidHorizontalAccuracy);
  REQUIRE(loc.get_gps_input_stats().accepted == 0);
  REQUIRE(loc.get_gps_input_stats().rejected_horizontal_accuracy == 1);
}

TEST_CASE("handle_gps accuracy semantics", "[pr75][gps][accuracy]") {
  Localizer loc;
  loc.reset_kalman(1.0);
  GpsFields base;
  seed_filter_near_gps(loc, 1.0, base);

  SECTION("negative horizontal accuracy rejected on UBLOX") {
    GpsFields f = base;
    f.horizontal_accuracy = -1.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidHorizontalAccuracy);
  }
  SECTION("zero vertical accuracy rejected") {
    GpsFields f = base;
    f.vertical_accuracy = 0.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidVerticalAccuracy);
  }
  SECTION("zero speed accuracy rejected") {
    GpsFields f = base;
    f.speed_accuracy = 0.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidSpeedAccuracy);
  }
  SECTION("zero bearing accuracy rejected") {
    GpsFields f = base;
    f.bearing_accuracy_deg = 0.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidBearingAccuracy);
  }
  SECTION("ordinary valid values accepted") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, base);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
    REQUIRE(loc.get_gps_input_stats().accepted == 1);
    REQUIRE(loc.is_gps_ok());
  }
}

TEST_CASE("non-finite current_time does not enter KF recovery", "[pr75][gps][time]") {
  Localizer loc;
  const double t0 = 1.0;
  loc.reset_kalman(t0);

  // Inflate position covariance so determine_gps_mode would otherwise call fake-GPS recovery.
  VectorXd x = loc.get_state();
  MatrixXdr P = loc.get_cov();
  P.block<STATE_ECEF_POS_ERR_LEN, STATE_ECEF_POS_ERR_LEN>(STATE_ECEF_POS_ERR_START, STATE_ECEF_POS_ERR_START).diagonal() =
      Vector3d::Constant(1e7);
  loc.reset_kalman(t0, x, P);

  GpsFields base;
  MessageBuilder msg;
  auto gps = msg.initEvent().initGpsLocationExternal();
  fill_gps(gps, base);

  VectorXd x_before = loc.get_state();
  MatrixXdr P_before = loc.get_cov();
  const uint64_t accepted_before = loc.get_gps_input_stats().accepted;

  loc.handle_gps(std::numeric_limits<double>::quiet_NaN(), gps.asReader(), kSensorOffset);

  REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NonFiniteInput);
  REQUIRE(loc.get_gps_input_stats().accepted == accepted_before);
  REQUIRE(loc.get_state().array().isFinite().all());
  REQUIRE(loc.get_cov().array().isFinite().all());
  REQUIRE(loc.get_state().isApprox(x_before));
  REQUIRE(loc.get_cov().isApprox(P_before));
}

TEST_CASE("handle_gps rejection diagnostics classify reasons", "[pr75][gps][diagnostics]") {
  Localizer loc;
  loc.reset_kalman(1.0);
  GpsFields base;
  seed_filter_near_gps(loc, 1.0, base);

  SECTION("no fix") {
    GpsFields f = base;
    f.has_fix = false;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::NoFix);
    REQUIRE(loc.get_gps_input_stats().rejected_no_fix == 1);
  }
  SECTION("invalid lat/lon/alt") {
    GpsFields f = base;
    f.latitude = 95.0;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::InvalidLatLonAlt);
  }
  SECTION("unreasonable velocity") {
    GpsFields f = base;
    f.vn = 250.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::UnreasonableVelocity);
  }
  SECTION("unreasonable uncertainty") {
    GpsFields f = base;
    f.horizontal_accuracy = 2000.0f;
    f.vertical_accuracy = 2000.0f;
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, f);
    loc.handle_gps(2.0, gps.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::UnreasonableUncertainty);
  }
}

TEST_CASE("large-position reset heading safety", "[pr75][gps][heading]") {
  Localizer loc;
  const double t0 = 1.0;
  loc.reset_kalman(t0);

  GpsFields gps;
  gps.latitude = kValidLat;
  gps.longitude = kValidLon;
  gps.altitude = kValidAlt;
  gps.bearing_deg = 45.0f;
  gps.bearing_accuracy_deg = 5.0f;

  Geodetic far_geo = {kValidLat + 0.01, kValidLon + 0.01, kValidAlt};
  ECEF far_ecef = geodetic2ecef(far_geo);
  VectorXd x = loc.get_state();
  MatrixXdr P = loc.get_cov();
  x.segment<STATE_ECEF_POS_LEN>(STATE_ECEF_POS_START) = Vector3d(far_ecef.x, far_ecef.y, far_ecef.z);
  Vector3d orient_ned(0.0, 0.0, DEG2RAD(200.0));
  VectorXd orient_quat = quat_to_vector(euler2quat(ecef_euler_from_ned(far_ecef, orient_ned)));
  x.segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START) = orient_quat;
  loc.reset_kalman(t0, x, P);

  auto pos_err_to_gps = [&]() {
    Geodetic geo = {gps.latitude, gps.longitude, gps.altitude};
    ECEF ecef = geodetic2ecef(geo);
    Vector3d gps_pos(ecef.x, ecef.y, ecef.z);
    Vector3d filt_pos = loc.get_state().segment<STATE_ECEF_POS_LEN>(STATE_ECEF_POS_START);
    return (filt_pos - gps_pos).norm();
  };

  SECTION("reliable course at meaningful speed can initialize yaw") {
    gps.vn = 6.0f;
    gps.ve = 0.0f;
    gps.bearing_accuracy_deg = 5.0f;
    VectorXd orient_before = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    MessageBuilder msg;
    auto gps_msg = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps_msg, gps);
    loc.handle_gps(2.0, gps_msg.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
    REQUIRE(loc.gps_course_used_for_last_reset());
    REQUIRE(pos_err_to_gps() < 50.0);
    VectorXd orient_after = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    REQUIRE_FALSE(orient_after.isApprox(orient_before, 1e-6));
  }

  SECTION("low-speed course is not trusted for yaw reset") {
    gps.vn = 0.2f;
    gps.ve = 0.0f;
    gps.bearing_accuracy_deg = 5.0f;
    VectorXd orient_before = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    MessageBuilder msg;
    auto gps_msg = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps_msg, gps);
    loc.handle_gps(2.0, gps_msg.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
    REQUIRE_FALSE(loc.gps_course_used_for_last_reset());
    REQUIRE(pos_err_to_gps() < 50.0);
    VectorXd orient_after = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    REQUIRE(orient_after.isApprox(orient_before, 1e-6));
  }

  SECTION("poor bearing accuracy is not trusted for yaw reset") {
    gps.vn = 8.0f;
    gps.ve = 0.0f;
    gps.bearing_accuracy_deg = 80.0f;
    VectorXd orient_before = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    MessageBuilder msg;
    auto gps_msg = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps_msg, gps);
    loc.handle_gps(2.0, gps_msg.asReader(), kSensorOffset);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
    REQUIRE_FALSE(loc.gps_course_used_for_last_reset());
    REQUIRE(pos_err_to_gps() < 50.0);
    VectorXd orient_after = loc.get_state().segment<STATE_ECEF_ORIENTATION_LEN>(STATE_ECEF_ORIENTATION_START);
    REQUIRE(orient_after.isApprox(orient_before, 1e-6));
  }
}

TEST_CASE("PR80 source gating coordinates GPS authority", "[pr80][source]") {
  Localizer loc(LocalizerGnssSource::UBLOX);

  auto publish_state = [](Localizer &localizer, double t_s, cereal::GpsSourceState::SelectedSource selected,
                          uint32_t generation, double transition_s) {
    MessageBuilder st_msg;
    auto evt = st_msg.initEvent();
    evt.setLogMonoTime(static_cast<uint64_t>(t_s * 1e9));
    auto st = evt.initGpsSourceState();
    st.setSelected(selected);
    st.setGeneration(generation);
    st.setTransitionMonoNs(static_cast<uint64_t>(transition_s * 1e9));
    st.setTransitionReason("test");
    st.setUbloxHealth(cereal::GpsSourceState::SourceHealth::HEALTHY);
    st.setQcomHealth(cereal::GpsSourceState::SourceHealth::UNKNOWN);
    st.setUbloxHardwareAvailable(true);
    localizer.handle_msg(evt.asReader());
  };

  SECTION("no gpsSourceState rejects both sockets") {
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(2'000'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);
  }

  SECTION("fresh ubloxPrimary accepts ublox and rejects qcom") {
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.0);
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(2'200'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }

  SECTION("stale gpsSourceState expires authority") {
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.0);
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      // > 3s after last state recv
      evt.setLogMonoTime(6'000'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);
  }

  SECTION("source transition rejects pre-transition samples") {
    publish_state(loc, 5.0, cereal::GpsSourceState::SelectedSource::QCOM_FALLBACK, 1, 5.0);

    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder old_ublox;
      auto evt = old_ublox.initEvent();
      evt.setLogMonoTime(4'000'000'000ULL);  // before transition
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);

    {
      MessageBuilder stale_qcom;
      auto evt = stale_qcom.initEvent();
      evt.setLogMonoTime(5'000'000'000ULL);  // equal to transition -> reject
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);

    {
      MessageBuilder fresh_qcom;
      auto evt = fresh_qcom.initEvent();
      evt.setLogMonoTime(5'100'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }

  SECTION("arbiter restart same source/gen newer epoch invalidates old GPS") {
    publish_state(loc, 1.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.100);
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(1'500'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    const uint64_t mid = loc.get_gps_input_stats().received;
    REQUIRE(mid >= 1);

    // Restart: same selected+generation, newer transition epoch.
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.200);
    {
      MessageBuilder old_msg;
      auto evt = old_msg.initEvent();
      evt.setLogMonoTime(150'000'000ULL);  // 0.15s < new epoch 0.2s
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == mid);
    {
      MessageBuilder fresh_msg;
      auto evt = fresh_msg.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == mid + 1);
  }

  SECTION("noHealthySource rejects both sockets") {
    publish_state(loc, 3.0, cereal::GpsSourceState::SelectedSource::NO_HEALTHY_SOURCE, 2, 3.0);
    const uint64_t before = loc.get_gps_input_stats().received;
    for (bool ublox : {true, false}) {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(4'000'000'000ULL);
      if (ublox) {
        fill_gps(evt.initGpsLocationExternal(), {});
      } else {
        fill_gps(evt.initGpsLocation(), {});
      }
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);
  }

  SECTION("fresh state resumes after expiry") {
    publish_state(loc, 1.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.0);
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(5'000'000'000ULL);  // stale
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    const uint64_t before = loc.get_gps_input_stats().received;
    publish_state(loc, 5.1, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.0);
    {
      MessageBuilder msg;
      auto evt = msg.initEvent();
      evt.setLogMonoTime(5'200'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }

  SECTION("D: regressing epoch does not roll authority backward") {
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::QCOM_FALLBACK, 1, 0.100);
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder qcom;
      auto evt = qcom.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
    // Stale older epoch claiming ublox — reject; keep QCOM authority.
    publish_state(loc, 2.2, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 2, 0.050);
    {
      MessageBuilder ublox;
      auto evt = ublox.initEvent();
      evt.setLogMonoTime(2'300'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
    {
      MessageBuilder qcom2;
      auto evt = qcom2.initEvent();
      evt.setLogMonoTime(2'400'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 2);
  }

  SECTION("D: equal epoch inconsistent source/generation rejected") {
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::QCOM_FALLBACK, 1, 0.100);
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder qcom;
      auto evt = qcom.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
    publish_state(loc, 2.2, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 2, 0.100);
    {
      MessageBuilder ublox;
      auto evt = ublox.initEvent();
      evt.setLogMonoTime(2'300'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }

  SECTION("D: newer epoch with gen0 accepted as arbiter restart") {
    publish_state(loc, 1.0, cereal::GpsSourceState::SelectedSource::QCOM_FALLBACK, 1, 0.100);
    publish_state(loc, 2.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 0, 0.200);
    const uint64_t before = loc.get_gps_input_stats().received;
    {
      MessageBuilder ublox;
      auto evt = ublox.initEvent();
      evt.setLogMonoTime(2'100'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }

  SECTION("D: future epoch relative to receive time rejected") {
    publish_state(loc, 1.0, cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY, 1, 0.100);
    const uint64_t before = loc.get_gps_input_stats().received;
    // transition 5.0s > receive 1.5s
    publish_state(loc, 1.5, cereal::GpsSourceState::SelectedSource::QCOM_FALLBACK, 2, 5.0);
    {
      MessageBuilder qcom;
      auto evt = qcom.initEvent();
      evt.setLogMonoTime(1'600'000'000ULL);
      fill_gps(evt.initGpsLocation(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before);
    {
      MessageBuilder ublox;
      auto evt = ublox.initEvent();
      evt.setLogMonoTime(1'700'000'000ULL);
      fill_gps(evt.initGpsLocationExternal(), {});
      loc.handle_msg(evt.asReader());
    }
    REQUIRE(loc.get_gps_input_stats().received == before + 1);
  }
}

TEST_CASE("PR81 measurement timing and covariance", "[pr81][timing][covariance]") {
  Localizer loc(LocalizerGnssSource::UBLOX);
  GpsFields base;
  seed_filter_near_gps(loc, 1.0, base);

  auto fill_with_meas = [](cereal::GpsLocationData::Builder gps, const GpsFields &f, uint64_t meas_ns) {
    fill_gps(gps, f);
    gps.setMeasurementMonoNs(meas_ns);
  };

  SECTION("measurementMonoNs preferred over empirical offset") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    // Event at 10.0s; measurement at 9.7s — not 10.0-0.095=9.905
    fill_with_meas(gps, base, 9'700'000'000ULL);
    loc.handle_gps(10.0, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
  }

  SECTION("legacy measurementMonoNs unset uses offset but still KF-rewind safe") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_gps(gps, base);
    gps.setMeasurementMonoNs(0);  // legacy unset
    // Event 10.0, offset 0.095 → sensor_time 9.905; within rewind of filter seeded at 1.0 after warm.
    loc.handle_gps(10.0, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);

    // Filter far ahead of legacy observation → reject stale via KF rewind gate.
    loc.reset_kalman(12.0);
    seed_filter_near_gps(loc, 12.0, base);
    MessageBuilder stale;
    auto gps2 = stale.initEvent().initGpsLocationExternal();
    fill_gps(gps2, base);
    gps2.setMeasurementMonoNs(0);
    // current_time 10.0, offset 0.095 → 9.905; filter at 12.0 → >0.8s behind filter
    loc.handle_gps(10.0, gps2.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::StaleMeasurement);
  }

  SECTION("future measurement rejected") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_with_meas(gps, base, 11'000'000'000ULL);  // after event 10.0
    loc.handle_gps(10.0, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::FutureMeasurement);
  }

  SECTION("stale measurement beyond rewind rejected") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_with_meas(gps, base, 8'000'000'000ULL);  // 2.0s before event 10.0 > 0.8s
    loc.handle_gps(10.0, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::StaleMeasurement);
  }

  SECTION("pre-transition measurement rejected") {
    MessageBuilder st_msg;
    auto evt = st_msg.initEvent();
    evt.setLogMonoTime(5'000'000'000ULL);
    auto st = evt.initGpsSourceState();
    st.setSelected(cereal::GpsSourceState::SelectedSource::UBLOX_PRIMARY);
    st.setGeneration(1);
    st.setTransitionMonoNs(5'000'000'000ULL);
    st.setTransitionReason("test");
    st.setUbloxHealth(cereal::GpsSourceState::SourceHealth::HEALTHY);
    st.setQcomHealth(cereal::GpsSourceState::SourceHealth::UNKNOWN);
    st.setUbloxHardwareAvailable(true);
    loc.handle_msg(evt.asReader());

    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    // Within rewind window of event, but at/before transition epoch.
    fill_with_meas(gps, base, 4'900'000'000ULL);
    loc.handle_gps(5.5, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::PreTransitionMeasurement);
  }

  SECTION("delayed but within rewind accepted") {
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_with_meas(gps, base, 9'500'000'000ULL);  // 0.5s delay
    loc.handle_gps(10.0, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
  }

  SECTION("filter rewind: event fresh but filter ahead rejects") {
    // Advance filter to 101.2s then apply measurement at 100.0 with event 100.1.
    loc.reset_kalman(101.2);
    seed_filter_near_gps(loc, 101.2, base);
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_with_meas(gps, base, 100'000'000'000ULL);
    loc.handle_gps(100.1, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::StaleMeasurement);
  }

  SECTION("filter rewind: within window accepted") {
    // Build rewind history, then apply a mildly delayed measurement still within 0.8s.
    loc.reset_kalman(99.0);
    seed_filter_near_gps(loc, 99.0, base);
    {
      MessageBuilder warm;
      auto gps = warm.initEvent().initGpsLocationExternal();
      fill_with_meas(gps, base, 99'200'000'000ULL);
      loc.handle_gps(99.2, gps.asReader(), 0.095);
      REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
    }
    MessageBuilder msg;
    auto gps = msg.initEvent().initGpsLocationExternal();
    fill_with_meas(gps, base, 99'500'000'000ULL);
    loc.handle_gps(99.9, gps.asReader(), 0.095);
    REQUIRE(loc.get_gps_input_stats().last_reason == GpsInputRejectReason::Accepted);
  }
}
