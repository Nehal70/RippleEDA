// ALU checker with an independent reference model. SMOKE=1: per-op corner sweep. SMOKE=0: N random + corners.
`timescale 1ns/1ps
module tb_smoke;
  parameter int SMOKE = 1;
  parameter int N = 20000;
  parameter int SEED = 7;
  logic [15:0] a, b, y; logic [3:0] op; logic z, n, c, v;
  alu16 dut (.*);
  int passed = 0, total = 0, seed;
  string names [12] = '{"ADD","SUB","AND","OR","XOR","SLL","SRL","SRA","ROL","ROR","SLT","SLTU"};
  task automatic chk(input bit ok, input string s);
    total++; if (ok) passed++; else if (total - passed <= 12) $display("ERROR: %s", s);
  endtask
  task automatic ref_model(input logic [15:0] A, B, input logic [3:0] OP, output logic [15:0] Y, output logic C, V);
    logic [16:0] t; int s; s = B[3:0]; C = 0; V = 0;
    case (OP)
      0: begin t = A + B; Y = t[15:0]; C = t[16]; V = (A[15] == B[15]) && (Y[15] != A[15]); end
      1: begin t = {1'b0, A} - {1'b0, B}; Y = t[15:0]; C = (A < B); V = (A[15] != B[15]) && (Y[15] != A[15]); end
      2: Y = A & B;  3: Y = A | B;  4: Y = A ^ B;
      5: Y = A << s; 6: Y = A >> s;
      7: begin Y = A; for (int i = 0; i < s; i++) Y = {Y[15], Y[15:1]}; end
      8: begin Y = A; for (int i = 0; i < s; i++) Y = {Y[14:0], Y[15]}; end
      9: begin Y = A; for (int i = 0; i < s; i++) Y = {Y[0], Y[15:1]}; end
      10: Y = ($signed(A) < $signed(B)) ? 16'd1 : 16'd0;
      11: Y = (A < B) ? 16'd1 : 16'd0;
      default: Y = 0;
    endcase
  endtask
  task automatic one(input logic [15:0] A, B, input logic [3:0] OP);
    logic [15:0] Y; logic C, V; string tag;
    a = A; b = B; op = OP; #1;
    ref_model(A, B, OP, Y, C, V);
    tag = $sformatf("%s a=%04h b=%04h", names[OP], A, B);
    chk(y === Y, {tag, $sformatf(": y=%04h exp %04h", y, Y)});
    chk(z === (Y == 0), {tag, $sformatf(": z=%b exp %b", z, Y == 0)});
    chk(n === Y[15], {tag, ": n flag"});
    chk(c === C, {tag, $sformatf(": c=%b exp %b", c, C)});
    chk(v === V, {tag, $sformatf(": v=%b exp %b", v, V)});
  endtask
  logic [15:0] corners [8] = '{16'h0000, 16'h0001, 16'h7FFF, 16'h8000, 16'hFFFF, 16'h0100, 16'h00FF, 16'hAAAA};
  initial begin
    seed = SEED;
    for (int o = 0; o < 12; o++)
      for (int i = 0; i < 8; i++) for (int j = 0; j < 8; j++)
        if (SMOKE == 0 || (i + j) % 3 == 0 || o >= 5) one(corners[i], (o >= 5 && o <= 9) ? (16'(j * 2 + (i % 2)) | ((i % 2) ? 16'hA500 : 16'h0000)) : corners[j], o[3:0]);
    if (!SMOKE) for (int k = 0; k < N; k++) one($urandom(seed), $urandom(seed), $urandom(seed) % 12);
    $display("RESULT %0d %0d", passed, total);
    $finish;
  end
endmodule
