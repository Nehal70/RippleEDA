// ----------------------------------------------------------------------------
// SPEC: alu16
// 16-bit combinational ALU. op: 0 ADD, 1 SUB, 2 AND, 3 OR, 4 XOR, 5 SLL,
// 6 SRL, 7 SRA, 8 ROL, 9 ROR, 10 SLT (signed), 11 SLTU; shift and rotate
// amount is b[3:0]. Flags: z = (y == 0), n = y[15], c = carry-out for ADD and
// borrow for SUB (1 when a < b unsigned), v = signed overflow for ADD/SUB;
// c and v are 0 for all other ops.
// ----------------------------------------------------------------------------
module alu16 (
  input  logic [15:0] a,
  input  logic [15:0] b,
  input  logic [3:0]  op,
  output logic [15:0] y,
  output logic        z,
  output logic        n,
  output logic        c,
  output logic        v
);
  logic [16:0] sum, dif;
  logic [4:0]  sh, rsh;
  assign sh  = {1'b0, b[3:0]};
  assign rsh = 5'd16 - sh;
  assign sum = {1'b0, a} + {1'b0, b};
  assign dif = {1'b0, a} - {1'b0, b};

  always_comb begin
    y = '0; c = 1'b0; v = 1'b0;
    case (op)
      4'd0:  begin y = sum[15:0]; c = sum[16]; v = (a[15] == b[15]) && (sum[15] != a[15]); end
      4'd1:  begin y = dif[15:0]; c = ~dif[16]; v = (a[15] != b[15]) && (dif[15] != a[15]); end
      4'd2:  y = a & b;
      4'd3:  y = a | b;
      4'd4:  y = a ^ b;
      4'd5:  y = a << sh;
      4'd6:  y = a >> sh;
      4'd7:  y = a >> sh;
      4'd8:  y = (a << sh) | (a >> rsh);
      4'd9:  y = (a >> sh) | (a << rsh);
      4'd10: y = {15'd0, $signed(a) < $signed(b)};
      4'd11: y = {15'd0, $signed(a) < $signed(b)};
      default: y = '0;
    endcase
  end
  assign z = (y == 16'd0);
  assign n = y[15];
endmodule
