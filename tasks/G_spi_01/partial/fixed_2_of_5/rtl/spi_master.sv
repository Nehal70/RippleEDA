// SPI master: 8-bit transfers, CPOL/CPHA modes 0-3, MSB- or LSB-first, SCLK half-period = div clk cycles (div >= 1).
// cs_n is asserted one half-period before the first SCLK edge and released one half-period after the last.
module spi_master (
  input  logic       clk,
  input  logic       rst_n,
  input  logic       start,
  input  logic       cpol,
  input  logic       cpha,
  input  logic       lsb_first,
  input  logic [7:0] div,
  input  logic [7:0] tx,
  output logic       busy,
  output logic       done,
  output logic [7:0] rx,
  output logic       sclk,
  output logic       mosi,
  output logic       cs_n,
  input  logic       miso
);
  typedef enum logic [1:0] {IDLE, SETUP, XFER, HOLD} st_t;
  st_t        st;
  logic [3:0] cnt, d;
  logic [7:0] sh_tx, sh_rx;
  logic [4:0] edges;
  logic [3:0] bit_tx, bit_rx;
  logic       pha, lsb;

  function automatic logic [2:0] pos(input logic l, input logic [3:0] k);
    pos = l ? k[2:0] : 3'd7 - k[2:0];
  endfunction
  function automatic logic [2:0] rpos(input logic l, input logic [3:0] k);
    rpos = 3'd7 - k[2:0];
  endfunction

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      st <= IDLE; cnt <= '0; d <= 4'd1; sh_tx <= '0; sh_rx <= '0; edges <= '0; bit_tx <= '0; bit_rx <= '0;
      pha <= 1'b0; lsb <= 1'b0; sclk <= 1'b0; mosi <= 1'b0; cs_n <= 1'b1; busy <= 1'b0; done <= 1'b0; rx <= '0;
    end else begin
      case (st)
        IDLE: begin
          sclk <= cpol;
          if (start) begin
            pha <= cpha; lsb <= lsb_first; sh_tx <= tx; d <= (div == 8'd0) ? 4'd1 : div[3:0];
            cs_n <= 1'b0; busy <= 1'b1; cnt <= '0; edges <= '0; sh_rx <= '0; bit_rx <= '0;
            if (!cpha) begin mosi <= lsb_first ? tx[0] : tx[7]; bit_tx <= 4'd1; end
            else bit_tx <= 4'd0;
            st <= SETUP;
          end
        end
        SETUP: if (cnt == d - 4'd1) begin cnt <= '0; st <= XFER; end else cnt <= cnt + 4'd1;
        XFER: begin
          if (cnt == d - 4'd1) begin
            cnt   <= '0;
            sclk  <= ~sclk;
            edges <= edges + 5'd1;
            if (!edges[0]) begin                       // leading edge
              if (!pha) begin sh_rx[rpos(lsb, bit_rx)] <= miso; bit_rx <= bit_rx + 4'd1; end
              else      begin mosi <= sh_tx[pos(lsb, bit_tx)]; bit_tx <= bit_tx + 4'd1; end
            end else begin                             // trailing edge
              if (!pha) begin
                if (bit_tx < 4'd8) begin mosi <= sh_tx[pos(lsb, bit_tx)]; bit_tx <= bit_tx + 4'd1; end
              end else begin sh_rx[rpos(lsb, bit_rx)] <= miso; bit_rx <= bit_rx + 4'd1; end
            end
            if (edges == 5'd15) st <= HOLD;
          end else cnt <= cnt + 4'd1;
        end
        HOLD: if (cnt == d - 4'd1) begin
          cnt <= '0; cs_n <= 1'b1; busy <= 1'b0; done <= 1'b1; rx <= sh_rx; st <= IDLE;
        end else cnt <= cnt + 4'd1;
        default: st <= IDLE;
      endcase
    end
  end
endmodule
