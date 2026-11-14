// UART TX checker: decodes the line at mid-bit, checks framing, data and busy timing.
`timescale 1ns/1ps
module tb;
  parameter int CPB = 16;
  parameter int NBYTES = 24;
  logic clk = 0, rst_n = 0, tx_start = 0;
  logic [7:0] tx_data = 0;
  logic tx, tx_busy;
  uart_tx #(.CLKS_PER_BIT(CPB)) dut (.*);
  always #5 clk = ~clk;

  logic [7:0] sent [0:255];
  int nsent = 0, nrecv = 0, passed = 0, total = 0;
  task automatic chk(input bit c, input string s);
    total++; if (c) passed++; else if (total - passed <= 10) $display("ERROR: t=%0t %s", $time, s);
  endtask

  // driver: back-to-back frames, start pulse as soon as busy drops
  initial begin
    int t0;
    repeat (4) @(posedge clk); rst_n = 1;
    repeat (3) @(posedge clk);
    for (int i = 0; i < NBYTES; i++) begin
      @(negedge clk);
      tx_data = (i == 0) ? 8'h55 : (i == 1) ? 8'h80 : (i == 2) ? 8'h01 : $urandom;
      tx_start = 1; sent[nsent] = tx_data; nsent++;
      @(negedge clk); tx_start = 0; tx_data = 8'hxx;
      t0 = 1;
      while (tx_busy === 1'b1) begin @(negedge clk); t0++; end
      chk(t0 >= 10 * CPB - 1 && t0 <= 10 * CPB + 2, $sformatf("frame %0d busy for %0d cycles (exp ~%0d)", i, t0, 10 * CPB));
    end
    repeat (2 * CPB) @(posedge clk);
    chk(nrecv == NBYTES, $sformatf("received %0d of %0d frames", nrecv, NBYTES));
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end

  // receiver
  initial begin
    logic [7:0] b;
    @(posedge rst_n);
    forever begin
      @(negedge tx);
      repeat (CPB / 2) @(posedge clk);
      chk(tx === 1'b0, "start bit not low at mid-bit");
      for (int k = 0; k < 8; k++) begin repeat (CPB) @(posedge clk); b[k] = tx; end
      repeat (CPB) @(posedge clk);
      chk(tx === 1'b1, $sformatf("stop bit missing for frame %0d", nrecv));
      chk(b === sent[nrecv], $sformatf("frame %0d data exp %02h got %02h", nrecv, sent[nrecv], b));
      nrecv++;
    end
  end
  initial begin #(400 * CPB * NBYTES); $display("TIMEOUT"); $display("RESULT %0d %0d", passed, total + 1); $finish; end
endmodule
