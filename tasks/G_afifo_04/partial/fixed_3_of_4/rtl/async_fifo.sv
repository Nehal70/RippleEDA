// Dual-clock FIFO (Cummings style): binary+gray pointers, 2-flop synchronisers.
module async_fifo #(
  parameter int WIDTH = 8,
  parameter int AW    = 3
) (
  input  logic             wclk,
  input  logic             wrst_n,
  input  logic             winc,
  input  logic [WIDTH-1:0] wdata,
  output logic             wfull,
  input  logic             rclk,
  input  logic             rrst_n,
  input  logic             rinc,
  output logic [WIDTH-1:0] rdata,
  output logic             rempty
);
  logic [WIDTH-1:0] mem [2**AW];
  logic [AW:0] wbin, wgray, wbin_n, wgray_n, wq1_rgray, wq2_rgray;
  logic [AW:0] rbin, rgray, rbin_n, rgray_n, rq1_wgray, rq2_wgray;

  // ---------------- write domain
  assign wbin_n  = wbin + {{AW{1'b0}}, (winc && !wfull)};
  assign wgray_n = (wbin_n >> 1) ^ wbin_n;
  always_ff @(posedge wclk or negedge wrst_n) begin
    if (!wrst_n) begin
      wbin <= '0; wgray <= '0; wq1_rgray <= '0; wq2_rgray <= '0; wfull <= 1'b0;
    end else begin
      wbin <= wbin_n; wgray <= wgray_n;
      wq1_rgray <= rgray; wq2_rgray <= wq1_rgray;
      wfull <= (wgray_n == {~wq2_rgray[AW:AW-1], wq2_rgray[AW-2:0]});
    end
  end
  always_ff @(posedge wclk) if (winc && !wfull) mem[wbin[AW-1:0]] <= wdata;

  // ---------------- read domain
  assign rbin_n  = rbin + {{AW{1'b0}}, (rinc && !rempty)};
  assign rgray_n = (rbin_n >> 1) ^ rbin_n;
  always_ff @(posedge rclk or negedge rrst_n) begin
    if (!rrst_n) begin
      rbin <= '0; rgray <= '0; rq1_wgray <= '0; rq2_wgray <= '0; rempty <= 1'b1;
    end else begin
      rbin <= rbin_n; rgray <= rgray_n;
      rq1_wgray <= wgray; rq2_wgray <= rq1_wgray;
      rempty <= (rgray_n == rq2_wgray);
    end
  end
  assign rdata = mem[rbin_n[AW-1:0]];
endmodule
