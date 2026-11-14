// ----------------------------------------------------------------------------
// SPEC: uart_tx
// 8N1 UART transmitter (LSB first, idle high) parameterised by CLKS_PER_BIT
// (any value >= 2). tx_start while idle sends tx_data as one start bit (low),
// eight data bits LSB first and one stop bit (high), each lasting exactly
// CLKS_PER_BIT clock cycles. tx_busy covers the whole frame (start bit
// through stop bit) and frames may be sent back-to-back.
// ----------------------------------------------------------------------------
module uart_tx #(
  parameter int CLKS_PER_BIT = 16
) (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       tx_start,
  input  logic [7:0] tx_data,
  output logic       tx,
  output logic       tx_busy
);
  localparam int CW = (CLKS_PER_BIT > 1) ? $clog2(CLKS_PER_BIT) : 1;
  localparam logic [CW-1:0] LAST = CW'(CLKS_PER_BIT - 1);
  typedef enum logic [1:0] {IDLE, START, DATA, STOP} st_t;

  st_t           st;
  logic [CW-1:0] cnt;
  logic [2:0]    bitn;
  logic [7:0]    sh;

  assign tx_busy = (st != IDLE);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st <= IDLE; cnt <= '0; bitn <= '0; sh <= '0; tx <= 1'b1;
    end else begin
      case (st)
        IDLE: begin
          tx <= 1'b1;
          if (tx_start) begin sh <= tx_data; cnt <= '0; tx <= 1'b1; st <= START; end
        end
        START: begin
          if (cnt == LAST) begin cnt <= '0; bitn <= '0; tx <= sh[0]; st <= DATA; end
          else cnt <= cnt + 1'b1;
        end
        DATA: begin
          if (cnt == LAST) begin
            cnt <= '0;
            if (bitn == 3'd7) begin tx <= 1'b1; st <= STOP; end
            else begin bitn <= bitn + 1'b1; sh <= sh >> 1; tx <= sh[1]; end
          end else cnt <= cnt + 1'b1;
        end
        STOP: begin
          if (cnt == LAST) begin cnt <= '0; st <= IDLE; end
          else cnt <= cnt + 1'b1;
        end
        default: st <= IDLE;
      endcase
    end
  end
endmodule
