// Cycle-accurate reference model check. SMOKE=1: directed feature scenarios + short random. SMOKE=0: long random.
`timescale 1ns/1ps
module tb;
  parameter int SMOKE = 0;
  parameter int N = 20000;
  parameter int SEED = 7;
  logic clk = 0, rst_n = 0, ack = 0; logic [7:0] src = 0, en = 0, edge_mode = 0;
  logic irq; logic [2:0] id; logic [7:0] pending;
  irq_ctrl dut (.*);
  always #5 clk = ~clk;
  int passed = 0, total = 0, seed;
  logic [7:0] m_prev = 0, m_epend = 0;
  task automatic chk(input bit ok, input string s);
    total++; if (ok) passed++; else if (total - passed <= 12) $display("ERROR: t=%0t %s", $time, s);
  endtask
  function automatic logic [7:0] m_pending(); return (m_epend & edge_mode) | (src & ~edge_mode); endfunction
  // compare combinational outputs against the model before every clock edge, then advance the model
  task automatic step(input string what);
    logic [7:0] mp, mm, clr, rise; logic mi; logic [2:0] mid;
    #1;
    mp = m_pending(); mm = mp & en; mi = |mm; mid = 0;
    for (int i = 7; i >= 0; i--) if (mm[i]) mid = i;
    chk(pending === mp, $sformatf("%s: pending=%08b exp %08b", what, pending, mp));
    chk(irq === mi, $sformatf("%s: irq=%b exp %b", what, irq, mi));
    if (mi) chk(id === mid, $sformatf("%s: id=%0d exp %0d (pending&en=%08b)", what, id, mid, mm));
    clr = (ack && mi) ? (8'd1 << mid) : 8'd0; rise = src & ~m_prev;
    @(posedge clk);
    m_epend = (m_epend & ~clr) | (rise & edge_mode); m_prev = src;
    @(negedge clk);
  endtask
  task automatic drive(input logic [7:0] s, e, m, input logic a, input string what);
    src = s; en = e; edge_mode = m; ack = a; step(what);
  endtask
  initial begin
    seed = SEED;
    repeat (2) @(posedge clk); @(negedge clk); rst_n = 1;
    // edge sources latch on a rising edge and stay pending after src drops
    drive(8'h00, 8'hFF, 8'hFF, 0, "edge idle"); drive(8'h08, 8'hFF, 8'hFF, 0, "edge rise"); drive(8'h08, 8'hFF, 8'hFF, 0, "edge held");
    drive(8'h00, 8'hFF, 8'hFF, 0, "edge dropped, still pending"); drive(8'h00, 8'hFF, 8'hFF, 1, "ack edge source");
    drive(8'h08, 8'hFF, 8'hFF, 0, "src high again, no new edge"); drive(8'h08, 8'hFF, 8'hFF, 0, "no re-pend while high");
    drive(8'h00, 8'hFF, 8'hFF, 0, "low");
    // priority: lowest index wins; ack clears only the granted source
    drive(8'h24, 8'hFF, 8'hFF, 0, "two edges"); drive(8'h00, 8'hFF, 8'hFF, 0, "priority");
    drive(8'h00, 8'hFF, 8'hFF, 1, "ack lowest only"); drive(8'h00, 8'hFF, 8'hFF, 0, "other still pending");
    drive(8'h00, 8'hFF, 8'hFF, 1, "ack second"); drive(8'h00, 8'hFF, 8'hFF, 0, "none");
    // a disabled edge source still latches pending; enabling it later raises irq
    drive(8'h00, 8'h00, 8'hFF, 0, "disabled"); drive(8'h40, 8'h00, 8'hFF, 0, "edge while disabled"); drive(8'h00, 8'h00, 8'hFF, 0, "latched");
    drive(8'h00, 8'h40, 8'hFF, 0, "enable later"); drive(8'h00, 8'h40, 8'hFF, 1, "ack"); drive(8'h00, 8'h40, 8'hFF, 0, "cleared");
    // edge arriving in the same cycle as ack wins
    drive(8'h02, 8'hFF, 8'hFF, 0, "rise"); drive(8'h00, 8'hFF, 8'hFF, 0, "pending"); drive(8'h02, 8'hFF, 8'hFF, 1, "ack + new edge same cycle");
    drive(8'h02, 8'hFF, 8'hFF, 0, "re-pended"); drive(8'h00, 8'hFF, 8'hFF, 1, "ack"); drive(8'h00, 8'hFF, 8'hFF, 0, "idle");
    // level sources follow src and ignore ack
    drive(8'h10, 8'hFF, 8'h00, 0, "level high"); drive(8'h10, 8'hFF, 8'h00, 1, "ack level (no effect)");
    drive(8'h10, 8'hFF, 8'h00, 0, "level still"); drive(8'h00, 8'hFF, 8'h00, 0, "level low");
    for (int k = 0; k < (SMOKE ? 300 : N); k++)
      drive($urandom(seed), ($urandom(seed) % 4 == 0) ? $urandom(seed) : 8'hFF, ($urandom(seed) % 3 == 0) ? 8'h0F : 8'hFF,
            ($urandom(seed) % 3) == 0, $sformatf("random %0d", k));
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
endmodule
