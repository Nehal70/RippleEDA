// ----------------------------------------------------------------------------
// SPEC: mac_pipe
// 3-stage pipelined multiply-accumulate. A sample (a, b) presented with
// in_valid=1 produces out_valid=1 exactly three cycles later, at which point
// acc holds the running sum of the 8x8 products (24-bit accumulator).
// clear=1 on a sample makes acc restart from that sample's product; clear
// travels with its sample through the pipeline. acc holds its value between
// valid samples. Reset (async, active-low) clears acc and the valid pipeline.
// ----------------------------------------------------------------------------
module mac_pipe (
  input  logic        clk,
  input  logic        rst_n,
  input  logic        in_valid,
  input  logic        clear,
  input  logic [7:0]  a,
  input  logic [7:0]  b,
  output logic [23:0] acc,
  output logic        out_valid
);
  logic       v1, v2, c1, c2;
  logic [7:0] a1, b1;
  logic [15:0] p2;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      c1 <= 1'b0; c2 <= 1'b0;
      out_valid <= 1'b0; acc <= '0;
    end else begin
      v1 <= in_valid; c1 <= clear;
      v2 <= v1;       c2 <= c1;
      out_valid <= v2;
      if (v2) acc <= clear ? {8'd0, p2} : acc + {8'd0, p2};
    end
  end

  always_ff @(posedge clk) begin
    a1 <= a;
    b1 <= b;
    p2 <= a1 * b1;
  end
endmodule
