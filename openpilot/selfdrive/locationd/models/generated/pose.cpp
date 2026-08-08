#include "pose.h"

namespace {
#define DIM 18
#define EDIM 18
#define MEDIM 18
typedef void (*Hfun)(double *, double *, double *);
const static double MAHA_THRESH_4 = 7.814727903251177;
const static double MAHA_THRESH_10 = 7.814727903251177;
const static double MAHA_THRESH_13 = 7.814727903251177;
const static double MAHA_THRESH_14 = 7.814727903251177;

/******************************************************************************
 *                      Code generated with SymPy 1.14.0                      *
 *                                                                            *
 *              See http://www.sympy.org/ for more information.               *
 *                                                                            *
 *                         This file is part of 'ekf'                         *
 ******************************************************************************/
void err_fun(double *nom_x, double *delta_x, double *out_645782711982754757) {
   out_645782711982754757[0] = delta_x[0] + nom_x[0];
   out_645782711982754757[1] = delta_x[1] + nom_x[1];
   out_645782711982754757[2] = delta_x[2] + nom_x[2];
   out_645782711982754757[3] = delta_x[3] + nom_x[3];
   out_645782711982754757[4] = delta_x[4] + nom_x[4];
   out_645782711982754757[5] = delta_x[5] + nom_x[5];
   out_645782711982754757[6] = delta_x[6] + nom_x[6];
   out_645782711982754757[7] = delta_x[7] + nom_x[7];
   out_645782711982754757[8] = delta_x[8] + nom_x[8];
   out_645782711982754757[9] = delta_x[9] + nom_x[9];
   out_645782711982754757[10] = delta_x[10] + nom_x[10];
   out_645782711982754757[11] = delta_x[11] + nom_x[11];
   out_645782711982754757[12] = delta_x[12] + nom_x[12];
   out_645782711982754757[13] = delta_x[13] + nom_x[13];
   out_645782711982754757[14] = delta_x[14] + nom_x[14];
   out_645782711982754757[15] = delta_x[15] + nom_x[15];
   out_645782711982754757[16] = delta_x[16] + nom_x[16];
   out_645782711982754757[17] = delta_x[17] + nom_x[17];
}
void inv_err_fun(double *nom_x, double *true_x, double *out_5665805604113013196) {
   out_5665805604113013196[0] = -nom_x[0] + true_x[0];
   out_5665805604113013196[1] = -nom_x[1] + true_x[1];
   out_5665805604113013196[2] = -nom_x[2] + true_x[2];
   out_5665805604113013196[3] = -nom_x[3] + true_x[3];
   out_5665805604113013196[4] = -nom_x[4] + true_x[4];
   out_5665805604113013196[5] = -nom_x[5] + true_x[5];
   out_5665805604113013196[6] = -nom_x[6] + true_x[6];
   out_5665805604113013196[7] = -nom_x[7] + true_x[7];
   out_5665805604113013196[8] = -nom_x[8] + true_x[8];
   out_5665805604113013196[9] = -nom_x[9] + true_x[9];
   out_5665805604113013196[10] = -nom_x[10] + true_x[10];
   out_5665805604113013196[11] = -nom_x[11] + true_x[11];
   out_5665805604113013196[12] = -nom_x[12] + true_x[12];
   out_5665805604113013196[13] = -nom_x[13] + true_x[13];
   out_5665805604113013196[14] = -nom_x[14] + true_x[14];
   out_5665805604113013196[15] = -nom_x[15] + true_x[15];
   out_5665805604113013196[16] = -nom_x[16] + true_x[16];
   out_5665805604113013196[17] = -nom_x[17] + true_x[17];
}
void H_mod_fun(double *state, double *out_663618949091298669) {
   out_663618949091298669[0] = 1.0;
   out_663618949091298669[1] = 0.0;
   out_663618949091298669[2] = 0.0;
   out_663618949091298669[3] = 0.0;
   out_663618949091298669[4] = 0.0;
   out_663618949091298669[5] = 0.0;
   out_663618949091298669[6] = 0.0;
   out_663618949091298669[7] = 0.0;
   out_663618949091298669[8] = 0.0;
   out_663618949091298669[9] = 0.0;
   out_663618949091298669[10] = 0.0;
   out_663618949091298669[11] = 0.0;
   out_663618949091298669[12] = 0.0;
   out_663618949091298669[13] = 0.0;
   out_663618949091298669[14] = 0.0;
   out_663618949091298669[15] = 0.0;
   out_663618949091298669[16] = 0.0;
   out_663618949091298669[17] = 0.0;
   out_663618949091298669[18] = 0.0;
   out_663618949091298669[19] = 1.0;
   out_663618949091298669[20] = 0.0;
   out_663618949091298669[21] = 0.0;
   out_663618949091298669[22] = 0.0;
   out_663618949091298669[23] = 0.0;
   out_663618949091298669[24] = 0.0;
   out_663618949091298669[25] = 0.0;
   out_663618949091298669[26] = 0.0;
   out_663618949091298669[27] = 0.0;
   out_663618949091298669[28] = 0.0;
   out_663618949091298669[29] = 0.0;
   out_663618949091298669[30] = 0.0;
   out_663618949091298669[31] = 0.0;
   out_663618949091298669[32] = 0.0;
   out_663618949091298669[33] = 0.0;
   out_663618949091298669[34] = 0.0;
   out_663618949091298669[35] = 0.0;
   out_663618949091298669[36] = 0.0;
   out_663618949091298669[37] = 0.0;
   out_663618949091298669[38] = 1.0;
   out_663618949091298669[39] = 0.0;
   out_663618949091298669[40] = 0.0;
   out_663618949091298669[41] = 0.0;
   out_663618949091298669[42] = 0.0;
   out_663618949091298669[43] = 0.0;
   out_663618949091298669[44] = 0.0;
   out_663618949091298669[45] = 0.0;
   out_663618949091298669[46] = 0.0;
   out_663618949091298669[47] = 0.0;
   out_663618949091298669[48] = 0.0;
   out_663618949091298669[49] = 0.0;
   out_663618949091298669[50] = 0.0;
   out_663618949091298669[51] = 0.0;
   out_663618949091298669[52] = 0.0;
   out_663618949091298669[53] = 0.0;
   out_663618949091298669[54] = 0.0;
   out_663618949091298669[55] = 0.0;
   out_663618949091298669[56] = 0.0;
   out_663618949091298669[57] = 1.0;
   out_663618949091298669[58] = 0.0;
   out_663618949091298669[59] = 0.0;
   out_663618949091298669[60] = 0.0;
   out_663618949091298669[61] = 0.0;
   out_663618949091298669[62] = 0.0;
   out_663618949091298669[63] = 0.0;
   out_663618949091298669[64] = 0.0;
   out_663618949091298669[65] = 0.0;
   out_663618949091298669[66] = 0.0;
   out_663618949091298669[67] = 0.0;
   out_663618949091298669[68] = 0.0;
   out_663618949091298669[69] = 0.0;
   out_663618949091298669[70] = 0.0;
   out_663618949091298669[71] = 0.0;
   out_663618949091298669[72] = 0.0;
   out_663618949091298669[73] = 0.0;
   out_663618949091298669[74] = 0.0;
   out_663618949091298669[75] = 0.0;
   out_663618949091298669[76] = 1.0;
   out_663618949091298669[77] = 0.0;
   out_663618949091298669[78] = 0.0;
   out_663618949091298669[79] = 0.0;
   out_663618949091298669[80] = 0.0;
   out_663618949091298669[81] = 0.0;
   out_663618949091298669[82] = 0.0;
   out_663618949091298669[83] = 0.0;
   out_663618949091298669[84] = 0.0;
   out_663618949091298669[85] = 0.0;
   out_663618949091298669[86] = 0.0;
   out_663618949091298669[87] = 0.0;
   out_663618949091298669[88] = 0.0;
   out_663618949091298669[89] = 0.0;
   out_663618949091298669[90] = 0.0;
   out_663618949091298669[91] = 0.0;
   out_663618949091298669[92] = 0.0;
   out_663618949091298669[93] = 0.0;
   out_663618949091298669[94] = 0.0;
   out_663618949091298669[95] = 1.0;
   out_663618949091298669[96] = 0.0;
   out_663618949091298669[97] = 0.0;
   out_663618949091298669[98] = 0.0;
   out_663618949091298669[99] = 0.0;
   out_663618949091298669[100] = 0.0;
   out_663618949091298669[101] = 0.0;
   out_663618949091298669[102] = 0.0;
   out_663618949091298669[103] = 0.0;
   out_663618949091298669[104] = 0.0;
   out_663618949091298669[105] = 0.0;
   out_663618949091298669[106] = 0.0;
   out_663618949091298669[107] = 0.0;
   out_663618949091298669[108] = 0.0;
   out_663618949091298669[109] = 0.0;
   out_663618949091298669[110] = 0.0;
   out_663618949091298669[111] = 0.0;
   out_663618949091298669[112] = 0.0;
   out_663618949091298669[113] = 0.0;
   out_663618949091298669[114] = 1.0;
   out_663618949091298669[115] = 0.0;
   out_663618949091298669[116] = 0.0;
   out_663618949091298669[117] = 0.0;
   out_663618949091298669[118] = 0.0;
   out_663618949091298669[119] = 0.0;
   out_663618949091298669[120] = 0.0;
   out_663618949091298669[121] = 0.0;
   out_663618949091298669[122] = 0.0;
   out_663618949091298669[123] = 0.0;
   out_663618949091298669[124] = 0.0;
   out_663618949091298669[125] = 0.0;
   out_663618949091298669[126] = 0.0;
   out_663618949091298669[127] = 0.0;
   out_663618949091298669[128] = 0.0;
   out_663618949091298669[129] = 0.0;
   out_663618949091298669[130] = 0.0;
   out_663618949091298669[131] = 0.0;
   out_663618949091298669[132] = 0.0;
   out_663618949091298669[133] = 1.0;
   out_663618949091298669[134] = 0.0;
   out_663618949091298669[135] = 0.0;
   out_663618949091298669[136] = 0.0;
   out_663618949091298669[137] = 0.0;
   out_663618949091298669[138] = 0.0;
   out_663618949091298669[139] = 0.0;
   out_663618949091298669[140] = 0.0;
   out_663618949091298669[141] = 0.0;
   out_663618949091298669[142] = 0.0;
   out_663618949091298669[143] = 0.0;
   out_663618949091298669[144] = 0.0;
   out_663618949091298669[145] = 0.0;
   out_663618949091298669[146] = 0.0;
   out_663618949091298669[147] = 0.0;
   out_663618949091298669[148] = 0.0;
   out_663618949091298669[149] = 0.0;
   out_663618949091298669[150] = 0.0;
   out_663618949091298669[151] = 0.0;
   out_663618949091298669[152] = 1.0;
   out_663618949091298669[153] = 0.0;
   out_663618949091298669[154] = 0.0;
   out_663618949091298669[155] = 0.0;
   out_663618949091298669[156] = 0.0;
   out_663618949091298669[157] = 0.0;
   out_663618949091298669[158] = 0.0;
   out_663618949091298669[159] = 0.0;
   out_663618949091298669[160] = 0.0;
   out_663618949091298669[161] = 0.0;
   out_663618949091298669[162] = 0.0;
   out_663618949091298669[163] = 0.0;
   out_663618949091298669[164] = 0.0;
   out_663618949091298669[165] = 0.0;
   out_663618949091298669[166] = 0.0;
   out_663618949091298669[167] = 0.0;
   out_663618949091298669[168] = 0.0;
   out_663618949091298669[169] = 0.0;
   out_663618949091298669[170] = 0.0;
   out_663618949091298669[171] = 1.0;
   out_663618949091298669[172] = 0.0;
   out_663618949091298669[173] = 0.0;
   out_663618949091298669[174] = 0.0;
   out_663618949091298669[175] = 0.0;
   out_663618949091298669[176] = 0.0;
   out_663618949091298669[177] = 0.0;
   out_663618949091298669[178] = 0.0;
   out_663618949091298669[179] = 0.0;
   out_663618949091298669[180] = 0.0;
   out_663618949091298669[181] = 0.0;
   out_663618949091298669[182] = 0.0;
   out_663618949091298669[183] = 0.0;
   out_663618949091298669[184] = 0.0;
   out_663618949091298669[185] = 0.0;
   out_663618949091298669[186] = 0.0;
   out_663618949091298669[187] = 0.0;
   out_663618949091298669[188] = 0.0;
   out_663618949091298669[189] = 0.0;
   out_663618949091298669[190] = 1.0;
   out_663618949091298669[191] = 0.0;
   out_663618949091298669[192] = 0.0;
   out_663618949091298669[193] = 0.0;
   out_663618949091298669[194] = 0.0;
   out_663618949091298669[195] = 0.0;
   out_663618949091298669[196] = 0.0;
   out_663618949091298669[197] = 0.0;
   out_663618949091298669[198] = 0.0;
   out_663618949091298669[199] = 0.0;
   out_663618949091298669[200] = 0.0;
   out_663618949091298669[201] = 0.0;
   out_663618949091298669[202] = 0.0;
   out_663618949091298669[203] = 0.0;
   out_663618949091298669[204] = 0.0;
   out_663618949091298669[205] = 0.0;
   out_663618949091298669[206] = 0.0;
   out_663618949091298669[207] = 0.0;
   out_663618949091298669[208] = 0.0;
   out_663618949091298669[209] = 1.0;
   out_663618949091298669[210] = 0.0;
   out_663618949091298669[211] = 0.0;
   out_663618949091298669[212] = 0.0;
   out_663618949091298669[213] = 0.0;
   out_663618949091298669[214] = 0.0;
   out_663618949091298669[215] = 0.0;
   out_663618949091298669[216] = 0.0;
   out_663618949091298669[217] = 0.0;
   out_663618949091298669[218] = 0.0;
   out_663618949091298669[219] = 0.0;
   out_663618949091298669[220] = 0.0;
   out_663618949091298669[221] = 0.0;
   out_663618949091298669[222] = 0.0;
   out_663618949091298669[223] = 0.0;
   out_663618949091298669[224] = 0.0;
   out_663618949091298669[225] = 0.0;
   out_663618949091298669[226] = 0.0;
   out_663618949091298669[227] = 0.0;
   out_663618949091298669[228] = 1.0;
   out_663618949091298669[229] = 0.0;
   out_663618949091298669[230] = 0.0;
   out_663618949091298669[231] = 0.0;
   out_663618949091298669[232] = 0.0;
   out_663618949091298669[233] = 0.0;
   out_663618949091298669[234] = 0.0;
   out_663618949091298669[235] = 0.0;
   out_663618949091298669[236] = 0.0;
   out_663618949091298669[237] = 0.0;
   out_663618949091298669[238] = 0.0;
   out_663618949091298669[239] = 0.0;
   out_663618949091298669[240] = 0.0;
   out_663618949091298669[241] = 0.0;
   out_663618949091298669[242] = 0.0;
   out_663618949091298669[243] = 0.0;
   out_663618949091298669[244] = 0.0;
   out_663618949091298669[245] = 0.0;
   out_663618949091298669[246] = 0.0;
   out_663618949091298669[247] = 1.0;
   out_663618949091298669[248] = 0.0;
   out_663618949091298669[249] = 0.0;
   out_663618949091298669[250] = 0.0;
   out_663618949091298669[251] = 0.0;
   out_663618949091298669[252] = 0.0;
   out_663618949091298669[253] = 0.0;
   out_663618949091298669[254] = 0.0;
   out_663618949091298669[255] = 0.0;
   out_663618949091298669[256] = 0.0;
   out_663618949091298669[257] = 0.0;
   out_663618949091298669[258] = 0.0;
   out_663618949091298669[259] = 0.0;
   out_663618949091298669[260] = 0.0;
   out_663618949091298669[261] = 0.0;
   out_663618949091298669[262] = 0.0;
   out_663618949091298669[263] = 0.0;
   out_663618949091298669[264] = 0.0;
   out_663618949091298669[265] = 0.0;
   out_663618949091298669[266] = 1.0;
   out_663618949091298669[267] = 0.0;
   out_663618949091298669[268] = 0.0;
   out_663618949091298669[269] = 0.0;
   out_663618949091298669[270] = 0.0;
   out_663618949091298669[271] = 0.0;
   out_663618949091298669[272] = 0.0;
   out_663618949091298669[273] = 0.0;
   out_663618949091298669[274] = 0.0;
   out_663618949091298669[275] = 0.0;
   out_663618949091298669[276] = 0.0;
   out_663618949091298669[277] = 0.0;
   out_663618949091298669[278] = 0.0;
   out_663618949091298669[279] = 0.0;
   out_663618949091298669[280] = 0.0;
   out_663618949091298669[281] = 0.0;
   out_663618949091298669[282] = 0.0;
   out_663618949091298669[283] = 0.0;
   out_663618949091298669[284] = 0.0;
   out_663618949091298669[285] = 1.0;
   out_663618949091298669[286] = 0.0;
   out_663618949091298669[287] = 0.0;
   out_663618949091298669[288] = 0.0;
   out_663618949091298669[289] = 0.0;
   out_663618949091298669[290] = 0.0;
   out_663618949091298669[291] = 0.0;
   out_663618949091298669[292] = 0.0;
   out_663618949091298669[293] = 0.0;
   out_663618949091298669[294] = 0.0;
   out_663618949091298669[295] = 0.0;
   out_663618949091298669[296] = 0.0;
   out_663618949091298669[297] = 0.0;
   out_663618949091298669[298] = 0.0;
   out_663618949091298669[299] = 0.0;
   out_663618949091298669[300] = 0.0;
   out_663618949091298669[301] = 0.0;
   out_663618949091298669[302] = 0.0;
   out_663618949091298669[303] = 0.0;
   out_663618949091298669[304] = 1.0;
   out_663618949091298669[305] = 0.0;
   out_663618949091298669[306] = 0.0;
   out_663618949091298669[307] = 0.0;
   out_663618949091298669[308] = 0.0;
   out_663618949091298669[309] = 0.0;
   out_663618949091298669[310] = 0.0;
   out_663618949091298669[311] = 0.0;
   out_663618949091298669[312] = 0.0;
   out_663618949091298669[313] = 0.0;
   out_663618949091298669[314] = 0.0;
   out_663618949091298669[315] = 0.0;
   out_663618949091298669[316] = 0.0;
   out_663618949091298669[317] = 0.0;
   out_663618949091298669[318] = 0.0;
   out_663618949091298669[319] = 0.0;
   out_663618949091298669[320] = 0.0;
   out_663618949091298669[321] = 0.0;
   out_663618949091298669[322] = 0.0;
   out_663618949091298669[323] = 1.0;
}
void f_fun(double *state, double dt, double *out_5731918013781344827) {
   out_5731918013781344827[0] = atan2((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), -(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]));
   out_5731918013781344827[1] = asin(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]));
   out_5731918013781344827[2] = atan2(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), -(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]));
   out_5731918013781344827[3] = dt*state[12] + state[3];
   out_5731918013781344827[4] = dt*state[13] + state[4];
   out_5731918013781344827[5] = dt*state[14] + state[5];
   out_5731918013781344827[6] = state[6];
   out_5731918013781344827[7] = state[7];
   out_5731918013781344827[8] = state[8];
   out_5731918013781344827[9] = state[9];
   out_5731918013781344827[10] = state[10];
   out_5731918013781344827[11] = state[11];
   out_5731918013781344827[12] = state[12];
   out_5731918013781344827[13] = state[13];
   out_5731918013781344827[14] = state[14];
   out_5731918013781344827[15] = state[15];
   out_5731918013781344827[16] = state[16];
   out_5731918013781344827[17] = state[17];
}
void F_fun(double *state, double dt, double *out_1718728437089448129) {
   out_1718728437089448129[0] = ((-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*cos(state[0])*cos(state[1]) - sin(state[0])*cos(dt*state[6])*cos(dt*state[7])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + ((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*cos(state[0])*cos(state[1]) - sin(dt*state[6])*sin(state[0])*cos(dt*state[7])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_1718728437089448129[1] = ((-sin(dt*state[6])*sin(dt*state[8]) - sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*cos(state[1]) - (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*sin(state[1]) - sin(state[1])*cos(dt*state[6])*cos(dt*state[7])*cos(state[0]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*sin(state[1]) + (-sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) + sin(dt*state[8])*cos(dt*state[6]))*cos(state[1]) - sin(dt*state[6])*sin(state[1])*cos(dt*state[7])*cos(state[0]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_1718728437089448129[2] = 0;
   out_1718728437089448129[3] = 0;
   out_1718728437089448129[4] = 0;
   out_1718728437089448129[5] = 0;
   out_1718728437089448129[6] = (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(dt*cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*sin(dt*state[8]) - dt*sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-dt*sin(dt*state[6])*cos(dt*state[8]) + dt*sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) - dt*cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (dt*sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_1718728437089448129[7] = (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[6])*sin(dt*state[7])*cos(state[0])*cos(state[1]) + dt*sin(dt*state[6])*sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) - dt*sin(dt*state[6])*sin(state[1])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[7])*cos(dt*state[6])*cos(state[0])*cos(state[1]) + dt*sin(dt*state[8])*sin(state[0])*cos(dt*state[6])*cos(dt*state[7])*cos(state[1]) - dt*sin(state[1])*cos(dt*state[6])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_1718728437089448129[8] = ((dt*sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + dt*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (dt*sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + ((dt*sin(dt*state[6])*sin(dt*state[8]) + dt*sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*cos(dt*state[8]) + dt*sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_1718728437089448129[9] = 0;
   out_1718728437089448129[10] = 0;
   out_1718728437089448129[11] = 0;
   out_1718728437089448129[12] = 0;
   out_1718728437089448129[13] = 0;
   out_1718728437089448129[14] = 0;
   out_1718728437089448129[15] = 0;
   out_1718728437089448129[16] = 0;
   out_1718728437089448129[17] = 0;
   out_1718728437089448129[18] = (-sin(dt*state[7])*sin(state[0])*cos(state[1]) - sin(dt*state[8])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_1718728437089448129[19] = (-sin(dt*state[7])*sin(state[1])*cos(state[0]) + sin(dt*state[8])*sin(state[0])*sin(state[1])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_1718728437089448129[20] = 0;
   out_1718728437089448129[21] = 0;
   out_1718728437089448129[22] = 0;
   out_1718728437089448129[23] = 0;
   out_1718728437089448129[24] = 0;
   out_1718728437089448129[25] = (dt*sin(dt*state[7])*sin(dt*state[8])*sin(state[0])*cos(state[1]) - dt*sin(dt*state[7])*sin(state[1])*cos(dt*state[8]) + dt*cos(dt*state[7])*cos(state[0])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_1718728437089448129[26] = (-dt*sin(dt*state[8])*sin(state[1])*cos(dt*state[7]) - dt*sin(state[0])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_1718728437089448129[27] = 0;
   out_1718728437089448129[28] = 0;
   out_1718728437089448129[29] = 0;
   out_1718728437089448129[30] = 0;
   out_1718728437089448129[31] = 0;
   out_1718728437089448129[32] = 0;
   out_1718728437089448129[33] = 0;
   out_1718728437089448129[34] = 0;
   out_1718728437089448129[35] = 0;
   out_1718728437089448129[36] = ((sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[7]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[7]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_1718728437089448129[37] = (-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(-sin(dt*state[7])*sin(state[2])*cos(state[0])*cos(state[1]) + sin(dt*state[8])*sin(state[0])*sin(state[2])*cos(dt*state[7])*cos(state[1]) - sin(state[1])*sin(state[2])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*(-sin(dt*state[7])*cos(state[0])*cos(state[1])*cos(state[2]) + sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1])*cos(state[2]) - sin(state[1])*cos(dt*state[7])*cos(dt*state[8])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_1718728437089448129[38] = ((-sin(state[0])*sin(state[2]) - sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (-sin(state[0])*sin(state[1])*sin(state[2]) - cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_1718728437089448129[39] = 0;
   out_1718728437089448129[40] = 0;
   out_1718728437089448129[41] = 0;
   out_1718728437089448129[42] = 0;
   out_1718728437089448129[43] = (-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(dt*(sin(state[0])*cos(state[2]) - sin(state[1])*sin(state[2])*cos(state[0]))*cos(dt*state[7]) - dt*(sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[7])*sin(dt*state[8]) - dt*sin(dt*state[7])*sin(state[2])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*(dt*(-sin(state[0])*sin(state[2]) - sin(state[1])*cos(state[0])*cos(state[2]))*cos(dt*state[7]) - dt*(sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[7])*sin(dt*state[8]) - dt*sin(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_1718728437089448129[44] = (dt*(sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*cos(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*sin(state[2])*cos(dt*state[7])*cos(state[1]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + (dt*(sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*cos(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[7])*cos(state[1])*cos(state[2]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_1718728437089448129[45] = 0;
   out_1718728437089448129[46] = 0;
   out_1718728437089448129[47] = 0;
   out_1718728437089448129[48] = 0;
   out_1718728437089448129[49] = 0;
   out_1718728437089448129[50] = 0;
   out_1718728437089448129[51] = 0;
   out_1718728437089448129[52] = 0;
   out_1718728437089448129[53] = 0;
   out_1718728437089448129[54] = 0;
   out_1718728437089448129[55] = 0;
   out_1718728437089448129[56] = 0;
   out_1718728437089448129[57] = 1;
   out_1718728437089448129[58] = 0;
   out_1718728437089448129[59] = 0;
   out_1718728437089448129[60] = 0;
   out_1718728437089448129[61] = 0;
   out_1718728437089448129[62] = 0;
   out_1718728437089448129[63] = 0;
   out_1718728437089448129[64] = 0;
   out_1718728437089448129[65] = 0;
   out_1718728437089448129[66] = dt;
   out_1718728437089448129[67] = 0;
   out_1718728437089448129[68] = 0;
   out_1718728437089448129[69] = 0;
   out_1718728437089448129[70] = 0;
   out_1718728437089448129[71] = 0;
   out_1718728437089448129[72] = 0;
   out_1718728437089448129[73] = 0;
   out_1718728437089448129[74] = 0;
   out_1718728437089448129[75] = 0;
   out_1718728437089448129[76] = 1;
   out_1718728437089448129[77] = 0;
   out_1718728437089448129[78] = 0;
   out_1718728437089448129[79] = 0;
   out_1718728437089448129[80] = 0;
   out_1718728437089448129[81] = 0;
   out_1718728437089448129[82] = 0;
   out_1718728437089448129[83] = 0;
   out_1718728437089448129[84] = 0;
   out_1718728437089448129[85] = dt;
   out_1718728437089448129[86] = 0;
   out_1718728437089448129[87] = 0;
   out_1718728437089448129[88] = 0;
   out_1718728437089448129[89] = 0;
   out_1718728437089448129[90] = 0;
   out_1718728437089448129[91] = 0;
   out_1718728437089448129[92] = 0;
   out_1718728437089448129[93] = 0;
   out_1718728437089448129[94] = 0;
   out_1718728437089448129[95] = 1;
   out_1718728437089448129[96] = 0;
   out_1718728437089448129[97] = 0;
   out_1718728437089448129[98] = 0;
   out_1718728437089448129[99] = 0;
   out_1718728437089448129[100] = 0;
   out_1718728437089448129[101] = 0;
   out_1718728437089448129[102] = 0;
   out_1718728437089448129[103] = 0;
   out_1718728437089448129[104] = dt;
   out_1718728437089448129[105] = 0;
   out_1718728437089448129[106] = 0;
   out_1718728437089448129[107] = 0;
   out_1718728437089448129[108] = 0;
   out_1718728437089448129[109] = 0;
   out_1718728437089448129[110] = 0;
   out_1718728437089448129[111] = 0;
   out_1718728437089448129[112] = 0;
   out_1718728437089448129[113] = 0;
   out_1718728437089448129[114] = 1;
   out_1718728437089448129[115] = 0;
   out_1718728437089448129[116] = 0;
   out_1718728437089448129[117] = 0;
   out_1718728437089448129[118] = 0;
   out_1718728437089448129[119] = 0;
   out_1718728437089448129[120] = 0;
   out_1718728437089448129[121] = 0;
   out_1718728437089448129[122] = 0;
   out_1718728437089448129[123] = 0;
   out_1718728437089448129[124] = 0;
   out_1718728437089448129[125] = 0;
   out_1718728437089448129[126] = 0;
   out_1718728437089448129[127] = 0;
   out_1718728437089448129[128] = 0;
   out_1718728437089448129[129] = 0;
   out_1718728437089448129[130] = 0;
   out_1718728437089448129[131] = 0;
   out_1718728437089448129[132] = 0;
   out_1718728437089448129[133] = 1;
   out_1718728437089448129[134] = 0;
   out_1718728437089448129[135] = 0;
   out_1718728437089448129[136] = 0;
   out_1718728437089448129[137] = 0;
   out_1718728437089448129[138] = 0;
   out_1718728437089448129[139] = 0;
   out_1718728437089448129[140] = 0;
   out_1718728437089448129[141] = 0;
   out_1718728437089448129[142] = 0;
   out_1718728437089448129[143] = 0;
   out_1718728437089448129[144] = 0;
   out_1718728437089448129[145] = 0;
   out_1718728437089448129[146] = 0;
   out_1718728437089448129[147] = 0;
   out_1718728437089448129[148] = 0;
   out_1718728437089448129[149] = 0;
   out_1718728437089448129[150] = 0;
   out_1718728437089448129[151] = 0;
   out_1718728437089448129[152] = 1;
   out_1718728437089448129[153] = 0;
   out_1718728437089448129[154] = 0;
   out_1718728437089448129[155] = 0;
   out_1718728437089448129[156] = 0;
   out_1718728437089448129[157] = 0;
   out_1718728437089448129[158] = 0;
   out_1718728437089448129[159] = 0;
   out_1718728437089448129[160] = 0;
   out_1718728437089448129[161] = 0;
   out_1718728437089448129[162] = 0;
   out_1718728437089448129[163] = 0;
   out_1718728437089448129[164] = 0;
   out_1718728437089448129[165] = 0;
   out_1718728437089448129[166] = 0;
   out_1718728437089448129[167] = 0;
   out_1718728437089448129[168] = 0;
   out_1718728437089448129[169] = 0;
   out_1718728437089448129[170] = 0;
   out_1718728437089448129[171] = 1;
   out_1718728437089448129[172] = 0;
   out_1718728437089448129[173] = 0;
   out_1718728437089448129[174] = 0;
   out_1718728437089448129[175] = 0;
   out_1718728437089448129[176] = 0;
   out_1718728437089448129[177] = 0;
   out_1718728437089448129[178] = 0;
   out_1718728437089448129[179] = 0;
   out_1718728437089448129[180] = 0;
   out_1718728437089448129[181] = 0;
   out_1718728437089448129[182] = 0;
   out_1718728437089448129[183] = 0;
   out_1718728437089448129[184] = 0;
   out_1718728437089448129[185] = 0;
   out_1718728437089448129[186] = 0;
   out_1718728437089448129[187] = 0;
   out_1718728437089448129[188] = 0;
   out_1718728437089448129[189] = 0;
   out_1718728437089448129[190] = 1;
   out_1718728437089448129[191] = 0;
   out_1718728437089448129[192] = 0;
   out_1718728437089448129[193] = 0;
   out_1718728437089448129[194] = 0;
   out_1718728437089448129[195] = 0;
   out_1718728437089448129[196] = 0;
   out_1718728437089448129[197] = 0;
   out_1718728437089448129[198] = 0;
   out_1718728437089448129[199] = 0;
   out_1718728437089448129[200] = 0;
   out_1718728437089448129[201] = 0;
   out_1718728437089448129[202] = 0;
   out_1718728437089448129[203] = 0;
   out_1718728437089448129[204] = 0;
   out_1718728437089448129[205] = 0;
   out_1718728437089448129[206] = 0;
   out_1718728437089448129[207] = 0;
   out_1718728437089448129[208] = 0;
   out_1718728437089448129[209] = 1;
   out_1718728437089448129[210] = 0;
   out_1718728437089448129[211] = 0;
   out_1718728437089448129[212] = 0;
   out_1718728437089448129[213] = 0;
   out_1718728437089448129[214] = 0;
   out_1718728437089448129[215] = 0;
   out_1718728437089448129[216] = 0;
   out_1718728437089448129[217] = 0;
   out_1718728437089448129[218] = 0;
   out_1718728437089448129[219] = 0;
   out_1718728437089448129[220] = 0;
   out_1718728437089448129[221] = 0;
   out_1718728437089448129[222] = 0;
   out_1718728437089448129[223] = 0;
   out_1718728437089448129[224] = 0;
   out_1718728437089448129[225] = 0;
   out_1718728437089448129[226] = 0;
   out_1718728437089448129[227] = 0;
   out_1718728437089448129[228] = 1;
   out_1718728437089448129[229] = 0;
   out_1718728437089448129[230] = 0;
   out_1718728437089448129[231] = 0;
   out_1718728437089448129[232] = 0;
   out_1718728437089448129[233] = 0;
   out_1718728437089448129[234] = 0;
   out_1718728437089448129[235] = 0;
   out_1718728437089448129[236] = 0;
   out_1718728437089448129[237] = 0;
   out_1718728437089448129[238] = 0;
   out_1718728437089448129[239] = 0;
   out_1718728437089448129[240] = 0;
   out_1718728437089448129[241] = 0;
   out_1718728437089448129[242] = 0;
   out_1718728437089448129[243] = 0;
   out_1718728437089448129[244] = 0;
   out_1718728437089448129[245] = 0;
   out_1718728437089448129[246] = 0;
   out_1718728437089448129[247] = 1;
   out_1718728437089448129[248] = 0;
   out_1718728437089448129[249] = 0;
   out_1718728437089448129[250] = 0;
   out_1718728437089448129[251] = 0;
   out_1718728437089448129[252] = 0;
   out_1718728437089448129[253] = 0;
   out_1718728437089448129[254] = 0;
   out_1718728437089448129[255] = 0;
   out_1718728437089448129[256] = 0;
   out_1718728437089448129[257] = 0;
   out_1718728437089448129[258] = 0;
   out_1718728437089448129[259] = 0;
   out_1718728437089448129[260] = 0;
   out_1718728437089448129[261] = 0;
   out_1718728437089448129[262] = 0;
   out_1718728437089448129[263] = 0;
   out_1718728437089448129[264] = 0;
   out_1718728437089448129[265] = 0;
   out_1718728437089448129[266] = 1;
   out_1718728437089448129[267] = 0;
   out_1718728437089448129[268] = 0;
   out_1718728437089448129[269] = 0;
   out_1718728437089448129[270] = 0;
   out_1718728437089448129[271] = 0;
   out_1718728437089448129[272] = 0;
   out_1718728437089448129[273] = 0;
   out_1718728437089448129[274] = 0;
   out_1718728437089448129[275] = 0;
   out_1718728437089448129[276] = 0;
   out_1718728437089448129[277] = 0;
   out_1718728437089448129[278] = 0;
   out_1718728437089448129[279] = 0;
   out_1718728437089448129[280] = 0;
   out_1718728437089448129[281] = 0;
   out_1718728437089448129[282] = 0;
   out_1718728437089448129[283] = 0;
   out_1718728437089448129[284] = 0;
   out_1718728437089448129[285] = 1;
   out_1718728437089448129[286] = 0;
   out_1718728437089448129[287] = 0;
   out_1718728437089448129[288] = 0;
   out_1718728437089448129[289] = 0;
   out_1718728437089448129[290] = 0;
   out_1718728437089448129[291] = 0;
   out_1718728437089448129[292] = 0;
   out_1718728437089448129[293] = 0;
   out_1718728437089448129[294] = 0;
   out_1718728437089448129[295] = 0;
   out_1718728437089448129[296] = 0;
   out_1718728437089448129[297] = 0;
   out_1718728437089448129[298] = 0;
   out_1718728437089448129[299] = 0;
   out_1718728437089448129[300] = 0;
   out_1718728437089448129[301] = 0;
   out_1718728437089448129[302] = 0;
   out_1718728437089448129[303] = 0;
   out_1718728437089448129[304] = 1;
   out_1718728437089448129[305] = 0;
   out_1718728437089448129[306] = 0;
   out_1718728437089448129[307] = 0;
   out_1718728437089448129[308] = 0;
   out_1718728437089448129[309] = 0;
   out_1718728437089448129[310] = 0;
   out_1718728437089448129[311] = 0;
   out_1718728437089448129[312] = 0;
   out_1718728437089448129[313] = 0;
   out_1718728437089448129[314] = 0;
   out_1718728437089448129[315] = 0;
   out_1718728437089448129[316] = 0;
   out_1718728437089448129[317] = 0;
   out_1718728437089448129[318] = 0;
   out_1718728437089448129[319] = 0;
   out_1718728437089448129[320] = 0;
   out_1718728437089448129[321] = 0;
   out_1718728437089448129[322] = 0;
   out_1718728437089448129[323] = 1;
}
void h_4(double *state, double *unused, double *out_418877726504601121) {
   out_418877726504601121[0] = state[6] + state[9];
   out_418877726504601121[1] = state[7] + state[10];
   out_418877726504601121[2] = state[8] + state[11];
}
void H_4(double *state, double *unused, double *out_7449706697236677096) {
   out_7449706697236677096[0] = 0;
   out_7449706697236677096[1] = 0;
   out_7449706697236677096[2] = 0;
   out_7449706697236677096[3] = 0;
   out_7449706697236677096[4] = 0;
   out_7449706697236677096[5] = 0;
   out_7449706697236677096[6] = 1;
   out_7449706697236677096[7] = 0;
   out_7449706697236677096[8] = 0;
   out_7449706697236677096[9] = 1;
   out_7449706697236677096[10] = 0;
   out_7449706697236677096[11] = 0;
   out_7449706697236677096[12] = 0;
   out_7449706697236677096[13] = 0;
   out_7449706697236677096[14] = 0;
   out_7449706697236677096[15] = 0;
   out_7449706697236677096[16] = 0;
   out_7449706697236677096[17] = 0;
   out_7449706697236677096[18] = 0;
   out_7449706697236677096[19] = 0;
   out_7449706697236677096[20] = 0;
   out_7449706697236677096[21] = 0;
   out_7449706697236677096[22] = 0;
   out_7449706697236677096[23] = 0;
   out_7449706697236677096[24] = 0;
   out_7449706697236677096[25] = 1;
   out_7449706697236677096[26] = 0;
   out_7449706697236677096[27] = 0;
   out_7449706697236677096[28] = 1;
   out_7449706697236677096[29] = 0;
   out_7449706697236677096[30] = 0;
   out_7449706697236677096[31] = 0;
   out_7449706697236677096[32] = 0;
   out_7449706697236677096[33] = 0;
   out_7449706697236677096[34] = 0;
   out_7449706697236677096[35] = 0;
   out_7449706697236677096[36] = 0;
   out_7449706697236677096[37] = 0;
   out_7449706697236677096[38] = 0;
   out_7449706697236677096[39] = 0;
   out_7449706697236677096[40] = 0;
   out_7449706697236677096[41] = 0;
   out_7449706697236677096[42] = 0;
   out_7449706697236677096[43] = 0;
   out_7449706697236677096[44] = 1;
   out_7449706697236677096[45] = 0;
   out_7449706697236677096[46] = 0;
   out_7449706697236677096[47] = 1;
   out_7449706697236677096[48] = 0;
   out_7449706697236677096[49] = 0;
   out_7449706697236677096[50] = 0;
   out_7449706697236677096[51] = 0;
   out_7449706697236677096[52] = 0;
   out_7449706697236677096[53] = 0;
}
void h_10(double *state, double *unused, double *out_5935648134991582197) {
   out_5935648134991582197[0] = 9.8100000000000005*sin(state[1]) - state[4]*state[8] + state[5]*state[7] + state[12] + state[15];
   out_5935648134991582197[1] = -9.8100000000000005*sin(state[0])*cos(state[1]) + state[3]*state[8] - state[5]*state[6] + state[13] + state[16];
   out_5935648134991582197[2] = -9.8100000000000005*cos(state[0])*cos(state[1]) - state[3]*state[7] + state[4]*state[6] + state[14] + state[17];
}
void H_10(double *state, double *unused, double *out_1352327652925264249) {
   out_1352327652925264249[0] = 0;
   out_1352327652925264249[1] = 9.8100000000000005*cos(state[1]);
   out_1352327652925264249[2] = 0;
   out_1352327652925264249[3] = 0;
   out_1352327652925264249[4] = -state[8];
   out_1352327652925264249[5] = state[7];
   out_1352327652925264249[6] = 0;
   out_1352327652925264249[7] = state[5];
   out_1352327652925264249[8] = -state[4];
   out_1352327652925264249[9] = 0;
   out_1352327652925264249[10] = 0;
   out_1352327652925264249[11] = 0;
   out_1352327652925264249[12] = 1;
   out_1352327652925264249[13] = 0;
   out_1352327652925264249[14] = 0;
   out_1352327652925264249[15] = 1;
   out_1352327652925264249[16] = 0;
   out_1352327652925264249[17] = 0;
   out_1352327652925264249[18] = -9.8100000000000005*cos(state[0])*cos(state[1]);
   out_1352327652925264249[19] = 9.8100000000000005*sin(state[0])*sin(state[1]);
   out_1352327652925264249[20] = 0;
   out_1352327652925264249[21] = state[8];
   out_1352327652925264249[22] = 0;
   out_1352327652925264249[23] = -state[6];
   out_1352327652925264249[24] = -state[5];
   out_1352327652925264249[25] = 0;
   out_1352327652925264249[26] = state[3];
   out_1352327652925264249[27] = 0;
   out_1352327652925264249[28] = 0;
   out_1352327652925264249[29] = 0;
   out_1352327652925264249[30] = 0;
   out_1352327652925264249[31] = 1;
   out_1352327652925264249[32] = 0;
   out_1352327652925264249[33] = 0;
   out_1352327652925264249[34] = 1;
   out_1352327652925264249[35] = 0;
   out_1352327652925264249[36] = 9.8100000000000005*sin(state[0])*cos(state[1]);
   out_1352327652925264249[37] = 9.8100000000000005*sin(state[1])*cos(state[0]);
   out_1352327652925264249[38] = 0;
   out_1352327652925264249[39] = -state[7];
   out_1352327652925264249[40] = state[6];
   out_1352327652925264249[41] = 0;
   out_1352327652925264249[42] = state[4];
   out_1352327652925264249[43] = -state[3];
   out_1352327652925264249[44] = 0;
   out_1352327652925264249[45] = 0;
   out_1352327652925264249[46] = 0;
   out_1352327652925264249[47] = 0;
   out_1352327652925264249[48] = 0;
   out_1352327652925264249[49] = 0;
   out_1352327652925264249[50] = 1;
   out_1352327652925264249[51] = 0;
   out_1352327652925264249[52] = 0;
   out_1352327652925264249[53] = 1;
}
void h_13(double *state, double *unused, double *out_736871905422189959) {
   out_736871905422189959[0] = state[3];
   out_736871905422189959[1] = state[4];
   out_736871905422189959[2] = state[5];
}
void H_13(double *state, double *unused, double *out_160924511080023833) {
   out_160924511080023833[0] = 0;
   out_160924511080023833[1] = 0;
   out_160924511080023833[2] = 0;
   out_160924511080023833[3] = 1;
   out_160924511080023833[4] = 0;
   out_160924511080023833[5] = 0;
   out_160924511080023833[6] = 0;
   out_160924511080023833[7] = 0;
   out_160924511080023833[8] = 0;
   out_160924511080023833[9] = 0;
   out_160924511080023833[10] = 0;
   out_160924511080023833[11] = 0;
   out_160924511080023833[12] = 0;
   out_160924511080023833[13] = 0;
   out_160924511080023833[14] = 0;
   out_160924511080023833[15] = 0;
   out_160924511080023833[16] = 0;
   out_160924511080023833[17] = 0;
   out_160924511080023833[18] = 0;
   out_160924511080023833[19] = 0;
   out_160924511080023833[20] = 0;
   out_160924511080023833[21] = 0;
   out_160924511080023833[22] = 1;
   out_160924511080023833[23] = 0;
   out_160924511080023833[24] = 0;
   out_160924511080023833[25] = 0;
   out_160924511080023833[26] = 0;
   out_160924511080023833[27] = 0;
   out_160924511080023833[28] = 0;
   out_160924511080023833[29] = 0;
   out_160924511080023833[30] = 0;
   out_160924511080023833[31] = 0;
   out_160924511080023833[32] = 0;
   out_160924511080023833[33] = 0;
   out_160924511080023833[34] = 0;
   out_160924511080023833[35] = 0;
   out_160924511080023833[36] = 0;
   out_160924511080023833[37] = 0;
   out_160924511080023833[38] = 0;
   out_160924511080023833[39] = 0;
   out_160924511080023833[40] = 0;
   out_160924511080023833[41] = 1;
   out_160924511080023833[42] = 0;
   out_160924511080023833[43] = 0;
   out_160924511080023833[44] = 0;
   out_160924511080023833[45] = 0;
   out_160924511080023833[46] = 0;
   out_160924511080023833[47] = 0;
   out_160924511080023833[48] = 0;
   out_160924511080023833[49] = 0;
   out_160924511080023833[50] = 0;
   out_160924511080023833[51] = 0;
   out_160924511080023833[52] = 0;
   out_160924511080023833[53] = 0;
}
void h_14(double *state, double *unused, double *out_4290885130923551943) {
   out_4290885130923551943[0] = state[6];
   out_4290885130923551943[1] = state[7];
   out_4290885130923551943[2] = state[8];
}
void H_14(double *state, double *unused, double *out_7914248944177502224) {
   out_7914248944177502224[0] = 0;
   out_7914248944177502224[1] = 0;
   out_7914248944177502224[2] = 0;
   out_7914248944177502224[3] = 0;
   out_7914248944177502224[4] = 0;
   out_7914248944177502224[5] = 0;
   out_7914248944177502224[6] = 1;
   out_7914248944177502224[7] = 0;
   out_7914248944177502224[8] = 0;
   out_7914248944177502224[9] = 0;
   out_7914248944177502224[10] = 0;
   out_7914248944177502224[11] = 0;
   out_7914248944177502224[12] = 0;
   out_7914248944177502224[13] = 0;
   out_7914248944177502224[14] = 0;
   out_7914248944177502224[15] = 0;
   out_7914248944177502224[16] = 0;
   out_7914248944177502224[17] = 0;
   out_7914248944177502224[18] = 0;
   out_7914248944177502224[19] = 0;
   out_7914248944177502224[20] = 0;
   out_7914248944177502224[21] = 0;
   out_7914248944177502224[22] = 0;
   out_7914248944177502224[23] = 0;
   out_7914248944177502224[24] = 0;
   out_7914248944177502224[25] = 1;
   out_7914248944177502224[26] = 0;
   out_7914248944177502224[27] = 0;
   out_7914248944177502224[28] = 0;
   out_7914248944177502224[29] = 0;
   out_7914248944177502224[30] = 0;
   out_7914248944177502224[31] = 0;
   out_7914248944177502224[32] = 0;
   out_7914248944177502224[33] = 0;
   out_7914248944177502224[34] = 0;
   out_7914248944177502224[35] = 0;
   out_7914248944177502224[36] = 0;
   out_7914248944177502224[37] = 0;
   out_7914248944177502224[38] = 0;
   out_7914248944177502224[39] = 0;
   out_7914248944177502224[40] = 0;
   out_7914248944177502224[41] = 0;
   out_7914248944177502224[42] = 0;
   out_7914248944177502224[43] = 0;
   out_7914248944177502224[44] = 1;
   out_7914248944177502224[45] = 0;
   out_7914248944177502224[46] = 0;
   out_7914248944177502224[47] = 0;
   out_7914248944177502224[48] = 0;
   out_7914248944177502224[49] = 0;
   out_7914248944177502224[50] = 0;
   out_7914248944177502224[51] = 0;
   out_7914248944177502224[52] = 0;
   out_7914248944177502224[53] = 0;
}
#include <eigen3/Eigen/Dense>
#include <iostream>

typedef Eigen::Matrix<double, DIM, DIM, Eigen::RowMajor> DDM;
typedef Eigen::Matrix<double, EDIM, EDIM, Eigen::RowMajor> EEM;
typedef Eigen::Matrix<double, DIM, EDIM, Eigen::RowMajor> DEM;

void predict(double *in_x, double *in_P, double *in_Q, double dt) {
  typedef Eigen::Matrix<double, MEDIM, MEDIM, Eigen::RowMajor> RRM;

  double nx[DIM] = {0};
  double in_F[EDIM*EDIM] = {0};

  // functions from sympy
  f_fun(in_x, dt, nx);
  F_fun(in_x, dt, in_F);


  EEM F(in_F);
  EEM P(in_P);
  EEM Q(in_Q);

  RRM F_main = F.topLeftCorner(MEDIM, MEDIM);
  P.topLeftCorner(MEDIM, MEDIM) = (F_main * P.topLeftCorner(MEDIM, MEDIM)) * F_main.transpose();
  P.topRightCorner(MEDIM, EDIM - MEDIM) = F_main * P.topRightCorner(MEDIM, EDIM - MEDIM);
  P.bottomLeftCorner(EDIM - MEDIM, MEDIM) = P.bottomLeftCorner(EDIM - MEDIM, MEDIM) * F_main.transpose();

  P = P + dt*Q;

  // copy out state
  memcpy(in_x, nx, DIM * sizeof(double));
  memcpy(in_P, P.data(), EDIM * EDIM * sizeof(double));
}

// note: extra_args dim only correct when null space projecting
// otherwise 1
template <int ZDIM, int EADIM, bool MAHA_TEST>
void update(double *in_x, double *in_P, Hfun h_fun, Hfun H_fun, Hfun Hea_fun, double *in_z, double *in_R, double *in_ea, double MAHA_THRESHOLD) {
  typedef Eigen::Matrix<double, ZDIM, ZDIM, Eigen::RowMajor> ZZM;
  typedef Eigen::Matrix<double, ZDIM, DIM, Eigen::RowMajor> ZDM;
  typedef Eigen::Matrix<double, Eigen::Dynamic, EDIM, Eigen::RowMajor> XEM;
  //typedef Eigen::Matrix<double, EDIM, ZDIM, Eigen::RowMajor> EZM;
  typedef Eigen::Matrix<double, Eigen::Dynamic, 1> X1M;
  typedef Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor> XXM;

  double in_hx[ZDIM] = {0};
  double in_H[ZDIM * DIM] = {0};
  double in_H_mod[EDIM * DIM] = {0};
  double delta_x[EDIM] = {0};
  double x_new[DIM] = {0};


  // state x, P
  Eigen::Matrix<double, ZDIM, 1> z(in_z);
  EEM P(in_P);
  ZZM pre_R(in_R);

  // functions from sympy
  h_fun(in_x, in_ea, in_hx);
  H_fun(in_x, in_ea, in_H);
  ZDM pre_H(in_H);

  // get y (y = z - hx)
  Eigen::Matrix<double, ZDIM, 1> pre_y(in_hx); pre_y = z - pre_y;
  X1M y; XXM H; XXM R;
  if (Hea_fun){
    typedef Eigen::Matrix<double, ZDIM, EADIM, Eigen::RowMajor> ZAM;
    double in_Hea[ZDIM * EADIM] = {0};
    Hea_fun(in_x, in_ea, in_Hea);
    ZAM Hea(in_Hea);
    XXM A = Hea.transpose().fullPivLu().kernel();


    y = A.transpose() * pre_y;
    H = A.transpose() * pre_H;
    R = A.transpose() * pre_R * A;
  } else {
    y = pre_y;
    H = pre_H;
    R = pre_R;
  }
  // get modified H
  H_mod_fun(in_x, in_H_mod);
  DEM H_mod(in_H_mod);
  XEM H_err = H * H_mod;

  // Do mahalobis distance test
  if (MAHA_TEST){
    XXM a = (H_err * P * H_err.transpose() + R).inverse();
    double maha_dist = y.transpose() * a * y;
    if (maha_dist > MAHA_THRESHOLD){
      R = 1.0e16 * R;
    }
  }

  // Outlier resilient weighting
  double weight = 1;//(1.5)/(1 + y.squaredNorm()/R.sum());

  // kalman gains and I_KH
  XXM S = ((H_err * P) * H_err.transpose()) + R/weight;
  XEM KT = S.fullPivLu().solve(H_err * P.transpose());
  //EZM K = KT.transpose(); TODO: WHY DOES THIS NOT COMPILE?
  //EZM K = S.fullPivLu().solve(H_err * P.transpose()).transpose();
  //std::cout << "Here is the matrix rot:\n" << K << std::endl;
  EEM I_KH = Eigen::Matrix<double, EDIM, EDIM>::Identity() - (KT.transpose() * H_err);

  // update state by injecting dx
  Eigen::Matrix<double, EDIM, 1> dx(delta_x);
  dx  = (KT.transpose() * y);
  memcpy(delta_x, dx.data(), EDIM * sizeof(double));
  err_fun(in_x, delta_x, x_new);
  Eigen::Matrix<double, DIM, 1> x(x_new);

  // update cov
  P = ((I_KH * P) * I_KH.transpose()) + ((KT.transpose() * R) * KT);

  // copy out state
  memcpy(in_x, x.data(), DIM * sizeof(double));
  memcpy(in_P, P.data(), EDIM * EDIM * sizeof(double));
  memcpy(in_z, y.data(), y.rows() * sizeof(double));
}




}
extern "C" {

void pose_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_4, H_4, NULL, in_z, in_R, in_ea, MAHA_THRESH_4);
}
void pose_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_10, H_10, NULL, in_z, in_R, in_ea, MAHA_THRESH_10);
}
void pose_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_13, H_13, NULL, in_z, in_R, in_ea, MAHA_THRESH_13);
}
void pose_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_14, H_14, NULL, in_z, in_R, in_ea, MAHA_THRESH_14);
}
void pose_err_fun(double *nom_x, double *delta_x, double *out_645782711982754757) {
  err_fun(nom_x, delta_x, out_645782711982754757);
}
void pose_inv_err_fun(double *nom_x, double *true_x, double *out_5665805604113013196) {
  inv_err_fun(nom_x, true_x, out_5665805604113013196);
}
void pose_H_mod_fun(double *state, double *out_663618949091298669) {
  H_mod_fun(state, out_663618949091298669);
}
void pose_f_fun(double *state, double dt, double *out_5731918013781344827) {
  f_fun(state,  dt, out_5731918013781344827);
}
void pose_F_fun(double *state, double dt, double *out_1718728437089448129) {
  F_fun(state,  dt, out_1718728437089448129);
}
void pose_h_4(double *state, double *unused, double *out_418877726504601121) {
  h_4(state, unused, out_418877726504601121);
}
void pose_H_4(double *state, double *unused, double *out_7449706697236677096) {
  H_4(state, unused, out_7449706697236677096);
}
void pose_h_10(double *state, double *unused, double *out_5935648134991582197) {
  h_10(state, unused, out_5935648134991582197);
}
void pose_H_10(double *state, double *unused, double *out_1352327652925264249) {
  H_10(state, unused, out_1352327652925264249);
}
void pose_h_13(double *state, double *unused, double *out_736871905422189959) {
  h_13(state, unused, out_736871905422189959);
}
void pose_H_13(double *state, double *unused, double *out_160924511080023833) {
  H_13(state, unused, out_160924511080023833);
}
void pose_h_14(double *state, double *unused, double *out_4290885130923551943) {
  h_14(state, unused, out_4290885130923551943);
}
void pose_H_14(double *state, double *unused, double *out_7914248944177502224) {
  H_14(state, unused, out_7914248944177502224);
}
void pose_predict(double *in_x, double *in_P, double *in_Q, double dt) {
  predict(in_x, in_P, in_Q, dt);
}
}

const EKF pose = {
  .name = "pose",
  .kinds = { 4, 10, 13, 14 },
  .feature_kinds = {  },
  .f_fun = pose_f_fun,
  .F_fun = pose_F_fun,
  .err_fun = pose_err_fun,
  .inv_err_fun = pose_inv_err_fun,
  .H_mod_fun = pose_H_mod_fun,
  .predict = pose_predict,
  .hs = {
    { 4, pose_h_4 },
    { 10, pose_h_10 },
    { 13, pose_h_13 },
    { 14, pose_h_14 },
  },
  .Hs = {
    { 4, pose_H_4 },
    { 10, pose_H_10 },
    { 13, pose_H_13 },
    { 14, pose_H_14 },
  },
  .updates = {
    { 4, pose_update_4 },
    { 10, pose_update_10 },
    { 13, pose_update_13 },
    { 14, pose_update_14 },
  },
  .Hes = {
  },
  .sets = {
  },
  .extra_routines = {
  },
};

ekf_lib_init(pose)
