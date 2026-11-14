// SPI master checker. SMOKE=1: fixed sweep (4 modes x 2 bit orders x div {1,3,40}). SMOKE=0: N random transfers.
`timescale 1ns/1ps
module tb_smoke;
  parameter int SMOKE = 1;
  parameter int N = 300;
  parameter int SEED = 7;
  logic clk = 0, rst_n = 0, start = 0, cpol = 0, cpha = 0, lsb_first = 0, miso = 0;
  logic [7:0] div = 1, tx = 0;
  logic busy, done, sclk, mosi, cs_n; logic [7:0] rx;
  spi_master dut (.*);
  always #5 clk = ~clk;
  int passed = 0, total = 0;
  task automatic chk(input bit c, input string s);
    total++; if (c) passed++; else if (total - passed <= 12) $display("ERROR: t=%0t %s", $time, s);
  endtask

  // ---------------- SPI slave model (follows the configured mode)
  logic [7:0] s_tx, s_rx; int s_bit = 0, n_edges = 0; logic s_pol, s_pha, s_lsb;
  function automatic int p(input int k); return s_lsb ? k : 7 - k; endfunction
  always @(negedge cs_n) begin
    s_bit = 0; n_edges = 0; s_rx = 'x;
    if (!s_pha) miso = s_tx[p(0)];
  end
  always @(sclk) if (!cs_n && $time > 0) begin
    bit leading; leading = (sclk !== s_pol);
    n_edges++;
    if (leading) begin
      if (!s_pha) s_rx[p(s_bit)] = mosi;          // sample on leading
      else        miso = s_tx[p(s_bit)];          // drive on leading
    end else begin
      if (!s_pha) begin s_bit++; if (s_bit < 8) miso = s_tx[p(s_bit)]; end
      else begin s_rx[p(s_bit)] = mosi; s_bit++; end
    end
  end

  // first SCLK edge after cs_n falls (for the setup check)
  int first_edge_t = -1;
  always @(negedge cs_n) first_edge_t = -1;
  always @(sclk) if (!cs_n && first_edge_t < 0) first_edge_t = $time / 10;

  task automatic xfer(input logic pol, pha, lsb, input logic [7:0] dv, data, sdata);
    int t_cs, t_first, t_last, t_rise, half, k_wait; string tag;
    tag = $sformatf("mode%0d %s div=%0d", {pol, pha}, lsb ? "LSB" : "MSB", dv);
    @(negedge clk);
    cpol = pol; cpha = pha; lsb_first = lsb; div = dv; tx = data;
    s_pol = pol; s_pha = pha; s_lsb = lsb; s_tx = sdata;
    repeat (2) @(negedge clk);
    chk(sclk === pol, {tag, ": SCLK idle level must equal CPOL"});
    start = 1; @(negedge clk); start = 0;
    t_cs = $time / 10;
    k_wait = 0;
    while (cs_n !== 1'b1 && k_wait < 40 * (int'(dv) + 2)) begin @(posedge clk); k_wait++; end
    t_first = first_edge_t;
    t_rise = $time / 10;
    half = (dv == 0) ? 1 : dv;
    chk(t_first >= 0 && t_first - t_cs >= half - 1, {tag, $sformatf(": CS setup %0d < half period %0d", t_first - t_cs, half)});
    chk(n_edges == 16, {tag, $sformatf(": %0d SCLK edges (exp 16)", n_edges)});
    chk(s_rx === data, {tag, $sformatf(": slave received %02h exp %02h", s_rx, data)});
    @(negedge clk);
    chk(rx === sdata, {tag, $sformatf(": master rx %02h exp %02h", rx, sdata)});
    chk(busy === 1'b0, {tag, ": busy still high after cs_n released"});
    chk(sclk === pol, {tag, ": SCLK not back at idle level"});
  endtask

  // cs hold check: time from last SCLK edge to cs_n rising
  int last_edge_t = 0, hold_viol = 0;
  always @(sclk) if (!cs_n) last_edge_t = $time / 10;
  always @(posedge cs_n) if ($time > 100 && ($time / 10 - last_edge_t) < (div - 1)) hold_viol++;
  int done_count = 0; always @(posedge clk) if (done === 1'b1) done_count++;

  initial begin
    int k, seed; logic [7:0] dvs [3];
    dvs[0] = 1; dvs[1] = 3; dvs[2] = 40;
    s_pol = 0; s_pha = 0; s_lsb = 0; s_tx = 0;
    repeat (3) @(posedge clk); rst_n = 1;
    if (SMOKE) begin
      for (int m = 0; m < 4; m++) for (int l = 0; l < 2; l++) for (int d = 0; d < 3; d++)
        xfer(m[1], m[0], l[0], dvs[d], 8'hA5 ^ (m << 4) ^ d, 8'h3C + m * 17 + l * 5 + d);
      k = 24;
    end else begin
      seed = SEED; k = $urandom(seed);
      for (int i = 0; i < N; i++)
        xfer($urandom % 2, $urandom % 2, $urandom % 2, ($urandom % 4 == 0) ? 8'd1 + $urandom % 255 : 8'd1 + $urandom % 6, $urandom, $urandom);
      k = N;
    end
    repeat (5) @(negedge clk);
    chk(hold_viol == 0, $sformatf("%0d transfers released cs_n less than a half period after the last SCLK edge", hold_viol));
    chk(done_count == k, $sformatf("done pulsed %0d times for %0d transfers", done_count, k));
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
  initial begin #50000000; $display("TIMEOUT"); $display("RESULT %0d %0d", passed, total + 1); $finish; end
endmodule
