`timescale 1ns/1ps
module tb_smoke;
  logic clk = 0, rst_n = 0, start = 0, busy, done;
  logic [15:0] a = 0, q; logic [7:0] b = 0, r;
  div16 dut (.*);
  always #5 clk = ~clk;
  int passed = 0, total = 0;
  task automatic chk(input bit c, input string s);
    total++; if (c) passed++; else if (total - passed <= 10) $display("ERROR: t=%0t %s", $time, s);
  endtask
  initial begin
    #23 rst_n = 1;
    for (int k = 0; k < 90; k++) begin
      logic [15:0] ea, eq; logic [7:0] eb, er; int lat;
      ea = (k % 37 == 0) ? 16'hFFFF : $urandom;
      eb = (k % 23 == 0) ? 8'd0 : (k % 29 == 0) ? 8'd1 : (k % 31 == 0) ? 8'hFF : $urandom;
      eq = (eb == 0) ? 16'hFFFF : ea / eb;
      er = (eb == 0) ? ea[7:0] : ea % eb;
      @(negedge clk); while (busy === 1'b1) @(negedge clk);
      a = ea; b = eb; start = 1; @(negedge clk); start = 0; a = $urandom; b = $urandom;   // operands only valid with start
      lat = 1; while (done !== 1'b1 && lat < 40) begin @(negedge clk); lat++; end
      chk(done === 1'b1 && lat <= 20, $sformatf("op %0d: no done within 20 cycles (lat %0d)", k, lat));
      chk(q === eq && r === er, $sformatf("%0d / %0d: exp q=%0d r=%0d got q=%0d r=%0d", ea, eb, eq, er, q, r));
    end
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
endmodule
