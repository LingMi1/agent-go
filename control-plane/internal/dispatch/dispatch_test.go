package dispatch

import "testing"

// 背压：达到上限即拒绝（非阻塞），释放后可再准入。
func TestAdmit_Backpressure(t *testing.T) {
	d := New(2, nil, nil, nil, 0, nil)

	r1, ok1 := d.Admit()
	r2, ok2 := d.Admit()
	if !ok1 || !ok2 {
		t.Fatalf("expected first two admits to succeed")
	}
	if _, ok3 := d.Admit(); ok3 {
		t.Fatalf("expected third admit to be rejected (busy)")
	}
	r1() // 释放一个槽
	r3, ok4 := d.Admit()
	if !ok4 {
		t.Fatalf("expected admit to succeed after release")
	}
	r2()
	r3()
}
