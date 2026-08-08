#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void live_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_9(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_12(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_35(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_32(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_33(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_H(double *in_vec, double *out_6747326267635382092);
void live_err_fun(double *nom_x, double *delta_x, double *out_4112258390237620168);
void live_inv_err_fun(double *nom_x, double *true_x, double *out_7855612695672879198);
void live_H_mod_fun(double *state, double *out_8320807506785189466);
void live_f_fun(double *state, double dt, double *out_7446878133195489541);
void live_F_fun(double *state, double dt, double *out_8372744191636472221);
void live_h_4(double *state, double *unused, double *out_3215308863626737779);
void live_H_4(double *state, double *unused, double *out_4249761318563711308);
void live_h_9(double *state, double *unused, double *out_420577338038842251);
void live_H_9(double *state, double *unused, double *out_4008571671934120663);
void live_h_10(double *state, double *unused, double *out_8990414433434905312);
void live_H_10(double *state, double *unused, double *out_817105994672163524);
void live_h_12(double *state, double *unused, double *out_7483770396545661923);
void live_H_12(double *state, double *unused, double *out_769695089468250487);
void live_h_35(double *state, double *unused, double *out_3680638008086765906);
void live_H_35(double *state, double *unused, double *out_3515258121793264196);
void live_h_32(double *state, double *unused, double *out_7668943186930176848);
void live_H_32(double *state, double *unused, double *out_751934476250363463);
void live_h_13(double *state, double *unused, double *out_8479284216609531012);
void live_H_13(double *state, double *unused, double *out_4600301726006262427);
void live_h_14(double *state, double *unused, double *out_420577338038842251);
void live_H_14(double *state, double *unused, double *out_4008571671934120663);
void live_h_33(double *state, double *unused, double *out_5559086865347104859);
void live_H_33(double *state, double *unused, double *out_2267457743447753672);
void live_predict(double *in_x, double *in_P, double *in_Q, double dt);
}