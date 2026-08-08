#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void car_update_25(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_24(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_30(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_26(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_27(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_29(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_28(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_31(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_err_fun(double *nom_x, double *delta_x, double *out_5655186241111261871);
void car_inv_err_fun(double *nom_x, double *true_x, double *out_1973102422327614893);
void car_H_mod_fun(double *state, double *out_8457607071393981172);
void car_f_fun(double *state, double dt, double *out_2570592637638187677);
void car_F_fun(double *state, double dt, double *out_7876213060418982958);
void car_h_25(double *state, double *unused, double *out_2792315217051374742);
void car_H_25(double *state, double *unused, double *out_3049408808623787036);
void car_h_24(double *state, double *unused, double *out_8548129583908006301);
void car_H_24(double *state, double *unused, double *out_876759209618287470);
void car_h_30(double *state, double *unused, double *out_845134311202647608);
void car_H_30(double *state, double *unused, double *out_5567741767131035663);
void car_h_26(double *state, double *unused, double *out_2012878112278789345);
void car_H_26(double *state, double *unused, double *out_692094510250269188);
void car_h_27(double *state, double *unused, double *out_4915143782422690736);
void car_H_27(double *state, double *unused, double *out_3392978455330610752);
void car_h_29(double *state, double *unused, double *out_5553907206211201538);
void car_H_29(double *state, double *unused, double *out_6077973111445427847);
void car_h_28(double *state, double *unused, double *out_7132558773873011734);
void car_H_28(double *state, double *unused, double *out_995574094375897273);
void car_h_31(double *state, double *unused, double *out_4668243547197513583);
void car_H_31(double *state, double *unused, double *out_1318302612483620664);
void car_predict(double *in_x, double *in_P, double *in_Q, double dt);
void car_set_mass(double x);
void car_set_rotational_inertia(double x);
void car_set_center_to_front(double x);
void car_set_center_to_rear(double x);
void car_set_stiffness_front(double x);
void car_set_stiffness_rear(double x);
}