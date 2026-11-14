// Self-checking random testbench for sync_fifo. Prints "RESULT <passed> <total>".
`timescale 1ns/1ps
module tb_smoke;
  parameter int WIDTH = 8;
  parameter int DEPTH = 8;
  parameter int CYCLES = 900;
  logic clk = 0, rst_n = 0, wr_en = 0, rd_en = 0;
  logic [WIDTH-1:0] wdata = 0, rdata;
  logic full, empty;
  logic [$clog2(DEPTH):0] count;
  sync_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (.*);
  always #5 clk = ~clk;

  logic [WIDTH-1:0] q [0:4095];
  int head = 0, tail = 0, n = 0, passed = 0, total = 0, seed = 1;

  task automatic chk(input bit cond, input string what);
    total++;
    if (cond) passed++;
    else if (total - passed <= 10) $display("ERROR: t=%0t %s (model n=%0d, dut count=%0d full=%b empty=%b)", $time, what, n, count, full, empty);
  endtask

  initial begin
    repeat (3) @(posedge clk);
    rst_n = 1;
    for (int c = 0; c < CYCLES; c++) begin
      @(negedge clk);
      // phases: fill-heavy, drain-heavy, mixed
      case ((c / 300) % 3)
        0: begin wr_en = ($urandom(seed) % 10) < 8; rd_en = ($urandom % 10) < 2; end
        1: begin wr_en = ($urandom % 10) < 2; rd_en = ($urandom % 10) < 8; end
        default: begin wr_en = $urandom % 2; rd_en = $urandom % 2; end
      endcase
      wdata = $urandom;
      #1;
      chk(full  === (n == DEPTH), "full flag");
      chk(empty === (n == 0), "empty flag");
      chk(count === n[$clog2(DEPTH):0], "count");
      if (rd_en && n > 0) chk(rdata === q[head % 4096], $sformatf("rdata exp %0h got %0h", q[head % 4096], rdata));
      begin
        bit dw, dr;
        dw = wr_en && (n < DEPTH);
        dr = rd_en && (n > 0);
        @(posedge clk); #1;
        if (dr) begin head++; n--; end
        if (dw) begin q[tail % 4096] = wdata; tail++; n++; end
      end
    end
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
  initial begin #200000; $display("TIMEOUT"); $display("RESULT %0d %0d", passed, total + 1); $finish; end
endmodule
