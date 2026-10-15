# Xử lý lỗi xuất hoá đơn Misa khi nhiều thanh toán cùng lúc

## 1. Bối cảnh

**Luồng xuất hoá đơn** (app-api, `POST /v1/payments/callback`):

1. PayOS gọi callback. `processCallback` ghi payment transaction, chuyển đơn sang `paid` và xử lý sản phẩm.
2. Nếu payment profile bật `allow_export_invoice`, hệ thống gọi `CreateInvoiceForPayment`:
   - lấy token Misa;
   - lấy mẫu/ký hiệu hoá đơn (cache Redis, mỗi công ty một mẫu: youpass và IELTS 1984);
   - gọi `POST /invoice` để tạo và phát hành hoá đơn;
   - ghi `invoice_transaction_id`, `invoice_ref_id` và log `meta` vào payment transaction.
3. Xuất lỗi thì gửi Lark kênh payment và ghi lỗi vào `meta`. Admin xuất lại bằng `POST /v1/payment-transactions/:id/invoice` (`PublishInvoice`).

**Vấn đề:** Misa cấp số hoá đơn tuần tự theo từng ký hiệu. Khi nhiều thanh toán đến cùng lúc, nhiều request phát hành trên **cùng một ký hiệu** gọi Misa đồng thời, và Misa trả lỗi số hoá đơn không liên tục (`InvoiceNumberNotContinuous`). Hoá đơn đó không được phát hành và phải xuất tay.

**Các thông số liên quan:**

- Mỗi lần gọi Misa dùng timeout 600 giây, kèm tối đa 3 lần retry ở tầng HTTP client (`pkg/httpclient`). Trường hợp xấu nhất, một hoá đơn có thể mất nhiều phút.
- API chạy 2 đến 15 pod (HPA).
- `PublishInvoice` (xuất tay) cũng gọi Misa, nên có thể chạy đồng thời với luồng tự động.

## 2. Redis lock quanh lời gọi Misa

### Cách hoạt động

Giữ nguyên luồng hiện tại (mỗi callback một goroutine). Trước khi gọi Misa, phải giữ một khoá chung trên Redis mà mọi pod API dùng chung. Khoá được giữ suốt lời gọi Misa và nhả ngay khi Misa trả response, nên với mỗi công ty, tại một thời điểm chỉ có một lời gọi phát hành hoá đơn.

1. **Khoá theo công ty**, vì số hoá đơn tuần tự theo ký hiệu, mà mỗi công ty dùng một mẫu: `lock:misa_invoice:youpass`, `lock:misa_invoice:ielts1984`. Hoá đơn youpass và IELTS 1984 không chặn nhau.
2. **Lấy khoá với TTL 30 giây** bằng `TryAcquireEventLock` của `EventLock` có sẵn trong `pkg/redis` (Redis `SET NX` kèm TTL; giá trị là chuỗi ngẫu nhiên riêng cho mỗi lần giữ khoá). TTL dùng hằng số có sẵn `redis.DefaultLockTTL`. Không dùng `WithEventLock`, vì hàm này không lấy được khoá thì bỏ qua luôn chứ không chờ, và luôn giữ khoá đủ TTL.
3. **Chưa lấy được khoá** (có hoá đơn khác đang gọi Misa) thì chờ rồi thử lại: thử mỗi 0,5 đến 1 giây, có cộng thêm khoảng ngẫu nhiên, tối đa 1 phút.
4. **Lấy được khoá:**
   - đọc lại payment transaction; nếu đã có `invoice_transaction_id` thì bỏ qua;
   - gọi `CreateInvoiceForPayment`;
   - ghi `invoice_transaction_id`.
5. **Nhả khoá ngay khi xử lý xong**, dù Misa trả thành công hay lỗi, hoặc dừng sớm vì hoá đơn đã có sẵn.
   - Nhả bằng `ReleaseLock` của Redis client. Hàm này dùng Lua so sánh giá trị, nên không nhả nhầm khoá của tiến trình khác.
   - Nếu lúc nhả thấy khoá đã hết hạn thì ghi log lỗi. Đây là dấu hiệu một lời gọi Misa đã kéo dài quá TTL.

   Endpoint phát hành hoá đơn của Misa là đồng bộ: status HTTP và kết quả về cùng một response. Thời điểm sớm nhất có thể nhả khoá an toàn chính là lúc nhận response. Vì vậy khoảng cách giữa hai lần gọi Misa bằng thời gian xử lý của lần gọi trước. Ở môi trường Misa test, một hoá đơn mất khoảng 1 đến 3 giây.

   **TTL 30 giây là giới hạn an toàn:**
   - Pod chết khi đang giữ khoá thì tối đa 30 giây sau khoá tự hết hạn, và các hoá đơn khác tiếp tục.
   - Lời gọi nào kéo dài quá 30 giây thì khoá hết hạn giữa chừng, và hoá đơn tiếp theo có thể bắt đầu trong khi lời gọi trước chưa xong.
6. **Chờ quá thời gian tối đa** thì gửi Lark và không gọi Misa. Admin xuất tay.
7. **Bọc `PublishInvoice` bằng cùng khoá,** để xuất tay cũng không chạy đồng thời với luồng tự động.
8. **Redis lỗi** (không lấy được khoá vì lỗi kết nối): không gọi Misa, gửi Lark. Như vậy hệ thống không vô tình quay lại gọi đồng thời.

### Ưu điểm

- Chặn va chạm từ gốc trên mọi pod API, kể cả xuất tay qua `PublishInvoice`.
- Không thêm hạ tầng. Code nằm gọn trong luồng xuất hoá đơn hiện tại, chỉ thêm một hàm bọc lời gọi Misa.
- Dùng Redis đã có sẵn, đúng quy ước của repo (distributed lock bằng Redis cho thao tác cần tránh chạy trùng).
- Khoá theo công ty, nên các công ty không chặn nhau.
- TTL ngắn (30 giây) và dùng lại được `EventLock` có sẵn. Pod chết khi đang giữ khoá chỉ làm hàng chờ đứng tối đa 30 giây.
- Không có thời gian chờ thừa: hoá đơn sau được gọi ngay khi Misa trả response cho hoá đơn trước.

### Nhược điểm

- **Hoá đơn đang chờ khoá nằm trong bộ nhớ pod API.** Pod restart, deploy hay scale-down thì các hoá đơn đang chờ mất, không có cảnh báo. Luồng hiện tại cũng có rủi ro này, nhưng giờ thời gian chờ dài hơn nên khả năng xảy ra cao hơn. Cần có cách phát hiện giao dịch thiếu hoá đơn để xuất bù.
- **Khi nhiều đơn dồn lại,** các goroutine chờ lâu, và có thể vượt thời gian chờ tối đa thì phải xuất tay. Thông lượng mỗi công ty phụ thuộc thời gian Misa xử lý một hoá đơn: với khoảng 1 đến 3 giây mỗi hoá đơn thì được khoảng 20 đến 60 hoá đơn mỗi phút. Một đợt dồn vượt mức này sẽ có đơn chờ quá 1 phút. Mỗi goroutine đang chờ cũng giữ tài nguyên trong pod.
- **Lời gọi Misa kéo dài quá TTL 30 giây** (Misa chậm bất thường, hoặc HTTP client tự retry sau lỗi mạng hay HTTP khác 2xx) làm khoá hết hạn trong khi lời gọi trước chưa xong, và hai lời gọi có thể chồng nhau. Có log khi xảy ra. Nếu gặp thường xuyên thì gia hạn khoá trong lúc gọi, hoặc đặt timeout gọi Misa nhỏ hơn TTL.
- **Không giữ thứ tự thanh toán:** goroutine nào thử lấy khoá đúng lúc khoá vừa được nhả thì xuất trước.

## 3. Kết hợp Redis lock với retry 1 lần

Redis lock là cơ chế chính, ngăn gọi Misa đồng thời. Retry 1 lần khi Misa trả lỗi số hoá đơn không liên tục có thể thêm vào làm lưới an toàn cho các case hiếm mà khoá không che được: khoá hết hạn giữa chừng, hoặc Misa trả lỗi này vì nguyên nhân khác.

### Cách retry hoạt động

1. Gọi `CreateInvoiceForPayment` như bình thường (bên trong khoá).
2. Nếu lỗi trả về đúng mã `InvoiceNumberNotContinuous`:
   - gửi Lark: gặp lỗi số hoá đơn không liên tục, sẽ retry 1 lần;
   - chờ một khoảng ngẫu nhiên ngắn, rồi gọi `CreateInvoiceForPayment` thêm đúng 1 lần.
3. Retry thành công: gửi Lark báo thành công kèm mã hoá đơn, và ghi `invoice_transaction_id` như bình thường.
4. Retry thất bại: gửi Lark báo thất bại sau retry, rồi admin xuất tay.
5. Các lỗi khác không retry.

### Lưu ý khi triển khai

- **Phân biệt mã lỗi:** `CreateInvoiceForPayment` cần trả về lỗi phân biệt được mã lỗi Misa (lỗi có kiểu riêng hoặc sentinel error), thay vì so khớp chuỗi `"MISA invoice creation failed: ..."`. Mã lỗi Misa có thể nằm ở `ErrorCode` của response tổng hoặc ở `PublishInvoiceResult[0].ErrorCode`, nên cần kiểm tra cả hai chỗ.
- **Chuỗi mã lỗi chính xác:** lấy từ `meta` của các payment transaction lỗi trên production trước khi code.
- **Retry nên nằm trong khoá,** để lần retry cũng không chạy đồng thời với hoá đơn khác. Thời gian giữ khoá vì thế dài hơn trong case retry, nên cần cân nhắc so với TTL 30 giây.
- **Phải chắc lần gọi lỗi không để lại hoá đơn ở phía Misa:** mỗi lần gọi `CreateInvoiceForPayment` tạo một `RefID` mới. Nếu khi trả lỗi này Misa vẫn giữ bản nháp hoặc hoá đơn của lần đầu, retry sẽ tạo thêm một bản.
- **Quan sát:** mỗi case retry đều có tin Lark (bắt đầu retry, thành công hoặc thất bại), và `meta` ghi lại cả hai lần gọi.

Chi phí là thêm phần code retry vào luồng hiện tại, không thêm hạ tầng.
