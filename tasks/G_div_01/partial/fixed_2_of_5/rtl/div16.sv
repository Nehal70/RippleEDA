// ----------------------------------------------------------------------------
// SPEC: div16
// Unsigned 16/8 divider (radix-2 restoring) with a start/done handshake.
// start while not busy begins a division of a by b; operands are only valid
// in the start cycle. busy is high while working. When the result is ready,
// done pulses for one cycle and q (quotient) / r (remainder) are valid.
// Latency up to 20 cycles is allowed. Divide-by-zero: q = FFFF, r = a[7:0].
// ----------------------------------------------------------------------------
module div16 (
  input  logic        clk,
  input  logic        rst_n,
  input  logic        start,
  input  logic [15:0] a,
  input  logic [7:0]  b,
  output logic        busy,
  output logic        done,
  output logic [15:0] q,
  output logic [7:0]  r
);
  logic [15:0] dvd;      // remaining dividend bits (shift out MSB first)
  logic [7:0]  rem;      // partial remainder (< divisor)
  logic [7:0]  dsr;
  logic [4:0]  n;
  logic [8:0]  sh;
  logic [8:0]  diff;
  logic        ge;

  assign sh   = {rem, dvd[15]};
  assign ge   = (sh >= {1'b0, dsr});     // trial subtract succeeds
  assign diff = sh - {1'b0, dsr};

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      busy <= 1'b0; done <= 1'b0; q <= '0; r <= '0;
      dvd <= '0; rem <= '0; dsr <= '0; n <= '0;
    end else begin
      done <= 1'b0;
      if (start && !busy) begin
        dvd <= a; dsr <= b; rem <= '0; n <= 5'd16;
      end else if (busy) begin
        if (n != 5'd0) begin
          dvd <= {dvd[14:0], ~ge};                // quotient bits shift in at the bottom
          rem <= ge ? diff[7:0] : rem;
          n   <= n - 5'd1;
        end else begin
          busy <= 1'b0; done <= 1'b1;
          q <= dvd;          // b == 0: every trial subtract succeeds -> q = FFFF, r = a[7:0]
          r <= rem;
        end
      end
    end
  end
endmodule
