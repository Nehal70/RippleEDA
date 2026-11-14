`timescale 1ns/1ps
module tb;
  parameter int WPER = 10;
  parameter int RPER = 14;
  logic wclk = 0, rclk = 0, wrst_n = 0, rrst_n = 0, winc = 0, rinc = 0;
  logic [7:0] wdata = 0, rdata; logic wfull, rempty;
  async_fifo #(.WIDTH(8), .AW(3)) dut (.*);
  always #(WPER/2.0) wclk = ~wclk;
  always #(RPER/2.0) rclk = ~rclk;
  logic [7:0] q [0:65535]; int wr = 0, rd = 0, passed = 0, total = 0, inflight; bit wdone = 0; int idle = 0;
  task automatic chk(input bit c, input string s);
    total++; if (c) passed++; else if (total - passed <= 10) $display("ERROR: t=%0t %s", $time, s);
  endtask
  initial begin #33 wrst_n = 1; rrst_n = 1; end
  initial begin #2000000; $display("TIMEOUT"); $display("RESULT %0d %0d", passed, total + 1); $finish; end
  // writer
  initial begin
    @(posedge wrst_n);
    repeat (2000) begin
      @(negedge wclk);
      winc = ($urandom % 4) != 0; wdata = $urandom;
      #0.1;
      if (winc && !wfull) begin q[wr % 65536] = wdata; wr++; end
    end
    @(negedge wclk); winc = 0; wdone = 1;
  end
  // reader
  initial begin
    @(posedge rrst_n); #1;
    chk(rempty === 1'b1, "rempty must be 1 after reset");
    while (!(wdone && idle > 20)) begin
      @(negedge rclk);
      rinc = ($urandom % 3) != 0;
      #0.1;
      idle = (rempty === 1'b1) ? idle + 1 : 0;
      if (rinc && !rempty) begin
        chk(rdata === q[rd % 65536], $sformatf("read %0d exp %02h got %02h", rd, q[rd % 65536], rdata)); rd++;
        chk(rd <= wr, "read more than written (underflow)");
      end
    end
    @(negedge rclk); rinc = 0;
    chk(rd == wr, $sformatf("lost data: wrote %0d read %0d", wr, rd));
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
  // occupancy can never exceed depth
  always @(posedge wclk) if (wrst_n) begin inflight = wr - rd; if (inflight > 8) chk(0, $sformatf("overflow: %0d in flight", inflight)); end
endmodule
