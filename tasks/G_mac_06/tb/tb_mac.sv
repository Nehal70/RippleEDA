`timescale 1ns/1ps
module tb;
  logic clk = 0, rst_n = 0, in_valid = 0, clear = 0;
  logic [7:0] a = 0, b = 0;
  logic [23:0] acc; logic out_valid;
  mac_pipe dut (.*);
  always #5 clk = ~clk;
  logic [23:0] expq [0:8191]; int wr = 0, rd = 0, passed = 0, total = 0;
  logic [23:0] model = 0;
  task automatic chk(input bit c, input string s);
    total++; if (c) passed++; else if (total - passed <= 10) $display("ERROR: t=%0t %s", $time, s);
  endtask
  initial begin
    // reset is applied mid-cycle with garbage on inputs, like a real SoC
    in_valid = 1'bx; clear = 1'bx;
    #12 rst_n = 0; #20 rst_n = 1; in_valid = 0; clear = 0;
    for (int c = 0; c < 3000; c++) begin
      @(negedge clk);
      if (out_valid !== 1'b0 && out_valid !== 1'b1) begin $display("XPROP out_valid"); chk(0, "out_valid is X"); end
      if (out_valid === 1'b1) begin
        chk(acc === expq[rd], $sformatf("acc exp %0d got %0d (sample %0d)", expq[rd], acc, rd)); rd++;
      end
      in_valid = (c < 2900) && ($urandom % 3 != 0);
      clear = ($urandom % 9 == 0);
      a = $urandom; b = $urandom;
      if (in_valid) begin model = (clear ? 24'd0 : model) + a * b; expq[wr] = model; wr++; end
    end
    chk(rd == wr, $sformatf("%0d outputs for %0d inputs", rd, wr));
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
endmodule
