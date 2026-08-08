#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void pose_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_err_fun(double *nom_x, double *delta_x, double *out_645782711982754757);
void pose_inv_err_fun(double *nom_x, double *true_x, double *out_5665805604113013196);
void pose_H_mod_fun(double *state, double *out_663618949091298669);
void pose_f_fun(double *state, double dt, double *out_5731918013781344827);
void pose_F_fun(double *state, double dt, double *out_1718728437089448129);
void pose_h_4(double *state, double *unused, double *out_418877726504601121);
void pose_H_4(double *state, double *unused, double *out_7449706697236677096);
void pose_h_10(double *state, double *unused, double *out_5935648134991582197);
void pose_H_10(double *state, double *unused, double *out_1352327652925264249);
void pose_h_13(double *state, double *unused, double *out_736871905422189959);
void pose_H_13(double *state, double *unused, double *out_160924511080023833);
void pose_h_14(double *state, double *unused, double *out_4290885130923551943);
void pose_H_14(double *state, double *unused, double *out_7914248944177502224);
void pose_predict(double *in_x, double *in_P, double *in_Q, double dt);
}