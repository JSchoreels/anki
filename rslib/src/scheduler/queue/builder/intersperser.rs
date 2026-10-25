// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/// Adapter to evenly mix two iterators of varying lengths into one.
pub(crate) struct Intersperser<I, I2>
where
    I: Iterator + ExactSizeIterator,
{
    one: I,
    two: I2,
    one_idx: usize,
    two_idx: usize,
    one_len: usize,
    two_len: usize,
    ratio: f32,
}

impl<I, I2> Intersperser<I, I2>
where
    I: ExactSizeIterator,
    I2: ExactSizeIterator<Item = I::Item>,
{
    pub fn new(one: I, two: I2) -> Self {
        Self::with_progress(one, two, 0, 0)
    }

    /// Resume mixing after entries from either source have already been
    /// answered.
    pub fn with_progress(one: I, two: I2, one_answered: usize, two_answered: usize) -> Self {
        let one_len = one.len() + one_answered;
        let two_len = two.len() + two_answered;
        let ratio = (one_len + 1) as f32 / (two_len + 1) as f32;
        Intersperser {
            one,
            two,
            one_idx: one_answered,
            two_idx: two_answered,
            one_len,
            two_len,
            ratio,
        }
    }

    fn one_idx(&self) -> Option<usize> {
        if self.one_idx == self.one_len {
            None
        } else {
            Some(self.one_idx)
        }
    }

    fn two_idx(&self) -> Option<usize> {
        if self.two_idx == self.two_len {
            None
        } else {
            Some(self.two_idx)
        }
    }

    fn next_one(&mut self) -> Option<I::Item> {
        self.one_idx += 1;
        self.one.next()
    }

    fn next_two(&mut self) -> Option<I::Item> {
        self.two_idx += 1;
        self.two.next()
    }
}

impl<I, I2> Iterator for Intersperser<I, I2>
where
    I: ExactSizeIterator,
    I2: ExactSizeIterator<Item = I::Item>,
{
    type Item = I::Item;

    fn next(&mut self) -> Option<Self::Item> {
        match (self.one_idx(), self.two_idx()) {
            (Some(idx1), Some(idx2)) => {
                let relative_idx2 = (idx2 + 1) as f32 * self.ratio;
                if relative_idx2 < (idx1 + 1) as f32 {
                    self.next_two()
                } else {
                    self.next_one()
                }
            }
            (Some(_), None) => self.next_one(),
            (None, Some(_)) => self.next_two(),
            (None, None) => None,
        }
    }

    fn size_hint(&self) -> (usize, Option<usize>) {
        let remaining = (self.one_len + self.two_len) - (self.one_idx + self.two_idx);
        (remaining, Some(remaining))
    }
}

impl<I, I2> ExactSizeIterator for Intersperser<I, I2>
where
    I: ExactSizeIterator,
    I2: ExactSizeIterator<Item = I::Item>,
{
}

#[cfg(test)]
mod test {
    use super::Intersperser;

    fn intersperse(a: &[u32], b: &[u32]) -> Vec<u32> {
        Intersperser::new(a.iter().cloned(), b.iter().cloned()).collect()
    }

    #[test]
    fn interspersing() {
        let a = &[1, 2, 3];
        let b = &[11, 22, 33];
        assert_eq!(&intersperse(a, b), &[1, 11, 2, 22, 3, 33]);

        let b = &[11, 22];
        assert_eq!(&intersperse(a, b), &[1, 11, 2, 22, 3]);

        // always add from longer iter first
        let b = &[11, 22, 33, 44, 55, 66];
        assert_eq!(&intersperse(a, b), &[11, 1, 22, 33, 2, 44, 55, 3, 66]);

        // space is distributed as evenly as possible between elements of
        // the same iter and start and end
        let b = &[11, 22, 33, 44, 55, 66, 77, 88];
        assert_eq!(
            &intersperse(a, b),
            &[11, 22, 1, 33, 44, 2, 55, 66, 3, 77, 88]
        );

        let b = &[];
        assert_eq!(&intersperse(a, b), &[1, 2, 3]);
    }

    #[test]
    fn resumed_mixing_preserves_the_remaining_order() {
        for (one_count, two_count) in [(20, 1), (20, 4), (2, 6)] {
            let one: Vec<_> = (1..=one_count).collect();
            let two: Vec<_> = (101..101 + two_count).collect();
            let expected = intersperse(&one, &two);
            for answered in 0..=expected.len() {
                let one_answered = expected[..answered].iter().filter(|&&id| id < 100).count();
                let two_answered = answered - one_answered;
                let remaining = Intersperser::with_progress(
                    one[one_answered..].iter().copied(),
                    two[two_answered..].iter().copied(),
                    one_answered,
                    two_answered,
                );
                assert_eq!(remaining.len(), expected.len() - answered);
                assert_eq!(remaining.collect::<Vec<_>>(), expected[answered..]);
            }
        }
    }
}
