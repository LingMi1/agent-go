package event

import "fmt"

// ValidateSequence 校验一个 run 的完整事件序列的跨事件不变量：
//   - 每条 Envelope 自身 Validate 通过；
//   - seq 严格 1..N 连续、无空洞（Python 分配、Go 在此自证）；
//   - finish 仅出现在 result 上，且整条序列至多一条 finish。
//
// 这是「重放==实时」的可执行闸门：回放前对 ListByRun 的结果调用它，既防御数据损坏，
// 也把契约不变量变成自证测试点。对已完成的 run，末条应为 result(finish)——该更强断言由调用方按需检查。
func ValidateSequence(events []Envelope) error {
	finishCount := 0
	for i, e := range events {
		if err := e.Validate(); err != nil {
			return fmt.Errorf("event[%d]: %w", i, err)
		}
		want := uint64(i + 1)
		if e.Seq != want {
			return fmt.Errorf("event: seq gap at index %d: got %d want %d", i, e.Seq, want)
		}
		if e.Finish {
			finishCount++
		}
	}
	if finishCount > 1 {
		return fmt.Errorf("event: at most one finish allowed, got %d", finishCount)
	}
	return nil
}
