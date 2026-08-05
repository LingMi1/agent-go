package event

import "testing"

func result(seq uint64) Envelope {
	return Envelope{Seq: seq, RunID: "r", MessageID: "res", Type: TypeResult, IsFinal: true, Finish: true,
		Result: &ResultPayload{Text: "done"}}
}

func thought(seq uint64) Envelope {
	return Envelope{Seq: seq, RunID: "r", MessageID: "th", Type: TypeToolThought, IsFinal: true,
		Thought: &ThoughtPayload{Text: "t"}}
}

func TestValidateSequence_OK(t *testing.T) {
	if err := ValidateSequence([]Envelope{thought(1), thought(2), result(3)}); err != nil {
		t.Fatalf("expected ok, got %v", err)
	}
}

func TestValidateSequence_Empty(t *testing.T) {
	if err := ValidateSequence(nil); err != nil {
		t.Fatalf("empty should pass, got %v", err)
	}
}

func TestValidateSequence_Gap(t *testing.T) {
	if err := ValidateSequence([]Envelope{thought(1), result(3)}); err == nil {
		t.Fatal("expected seq gap error")
	}
}

func TestValidateSequence_MultipleFinish(t *testing.T) {
	if err := ValidateSequence([]Envelope{result(1), result(2)}); err == nil {
		t.Fatal("expected multiple-finish error")
	}
}

func TestValidateSequence_FinishOnNonResult(t *testing.T) {
	// finish 落在非 result 上：被单条 Validate 捕获（finish iff result）。
	bad := Envelope{Seq: 1, RunID: "r", MessageID: "th", Type: TypeToolThought, IsFinal: true, Finish: true,
		Thought: &ThoughtPayload{Text: "t"}}
	if err := ValidateSequence([]Envelope{bad}); err == nil {
		t.Fatal("expected finish-on-non-result error")
	}
}
